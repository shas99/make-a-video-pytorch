import math
import functools
from operator import mul

import torch
from torch import nn, einsum

from einops import rearrange, pack, unpack
from einops.layers.torch import Rearrange

# helper functions

def exists(val):
    return val is not None

def default(val, d):
    return val if exists(val) else d

def mul_reduce(tup):
    return functools.reduce(mul, tup)

def divisible_by(numer, denom):
    return (numer % denom) == 0

mlist = nn.ModuleList

# for time conditioning

class SinusoidalPosEmb(nn.Module):
    def __init__(self, dim, theta = 10000):
        super().__init__()
        self.theta = theta
        self.dim = dim

    def forward(self, x):
        dtype, device = x.dtype, x.device
        assert dtype == torch.float, 'input to sinusoidal pos emb must be a float type'

        half_dim = self.dim // 2
        emb = math.log(self.theta) / (half_dim - 1)
        emb = torch.exp(torch.arange(half_dim, device = device, dtype = dtype) * -emb)
        emb = rearrange(x, 'i -> i 1') * rearrange(emb, 'j -> 1 j')
        return torch.cat((emb.sin(), emb.cos()), dim = -1).type(dtype)

# layernorm 3d

class LayerNorm(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.g = nn.Parameter(torch.ones(dim))

    def forward(self, x):
        eps = 1e-5 if x.dtype == torch.float32 else 1e-3
        var = torch.var(x, dim = 1, unbiased = False, keepdim = True)
        mean = torch.mean(x, dim = 1, keepdim = True)
        return (x - mean) * var.clamp(min = eps).rsqrt() * self.g

# feedforward

class GEGLU(nn.Module):
    def forward(self, x):
        x, gate = x.chunk(2, dim = -1)
        return x * F.gelu(gate)

def FeedForward(dim, mult = 4):
    inner_dim = int(dim * mult * 2 / 3)
    return nn.Sequential(
        nn.Linear(dim, inner_dim, bias = False),
        GEGLU(),
        nn.Linear(inner_dim, bias = False)
    )

# helper classes

class Attention(nn.Module):
    def __init__(
        self,
        dim,
        dim_head = 64,
        heads = 8
    ):
        super().__init__()
        self.heads = heads
        self.scale = dim_head ** -0.5
        inner_dim = dim_head * heads

        self.norm = LayerNorm(dim)

        self.to_q = nn.Linear(dim, inner_dim, bias = False)
        self.to_kv = nn.Linear(dim, inner_dim * 2, bias = False)
        self.to_out = nn.Linear(inner_dim, dim, bias = False)

        nn.init.zeros_(self.to_out.weight.data) # identity with skip connection

    def forward(self, x):
        x = self.norm(x)

        q, k, v = self.to_q(x), *self.to_kv(x).chunk(2, dim = -1)

        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h = self.heads), (q, k, v))

        q = q * self.scale

        sim = einsum('b h i d, b h j d -> b h i j', q, k)

        attn = sim.softmax(dim = -1)

        out = einsum('b h i j, b h j d -> b h i d', attn, v)

        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)

# main contribution - pseudo 3d conv

class PseudoConv3d(nn.Module):
    def __init__(
        self,
        dim,
        dim_out = None,
        kernel_size = 3,
        *,
        temporal_kernel_size = None,
        **kwargs
    ):
        super().__init__()
        dim_out = default(dim_out, dim)
        temporal_kernel_size = default(temporal_kernel_size, kernel_size)

        self.spatial_conv = nn.Conv2d(dim, dim_out, kernel_size = kernel_size, padding = kernel_size // 2)
        self.temporal_conv = nn.Conv1d(dim_out, dim_out, kernel_size = temporal_kernel_size, padding = temporal_kernel_size // 2) if kernel_size > 1 else None

        if exists(self.temporal_conv):
            nn.init.dirac_(self.temporal_conv.weight.data) # initialized to be identity
            nn.init.zeros_(self.temporal_conv.bias.data)

    def forward(
        self,
        x,
        enable_time = True
    ):
        b, c, *_, h, w = x.shape

        is_video = x.ndim == 5
        enable_time &= is_video

        if is_video:
            x = rearrange(x, 'b c f h w -> (b f) c h w')

        x = self.spatial_conv(x)

        if is_video:
            x = rearrange(x, '(b f) c h w -> b c f h w', b = b)

        if not enable_time or not exists(self.temporal_conv):
            return x

        x = rearrange(x, 'b c f h w -> (b h w) c f')

        x = self.temporal_conv(x)

        x = rearrange(x, '(b h w) c f -> b c f h w', h = h, w = w)

        return x

# factorized spatial temporal attention from Ho et al.
# todo - take care of relative positional biases + rotary embeddings

class SpatioTemporalAttention(nn.Module):
    def __init__(
        self,
        dim,
        *,
        dim_head = 64,
        heads = 8
    ):
        super().__init__()
        self.spatial_attn = Attention(dim = dim, dim_head = dim_head, heads = heads)
        self.temporal_attn = Attention(dim = dim, dim_head = dim_head, heads = heads)

    def forward(
        self,
        x,
        enable_time = True
    ):
        b, c, *_, h, w = x.shape
        is_video = x.ndim == 5
        enable_time &= is_video

        if is_video:
            x = rearrange(x, 'b c f h w -> (b f) (h w) c')
        else:
            x = rearrange(x, 'b c h w -> b (h w) c')

        x = self.spatial_attn(x) + x

        if is_video:
            x = rearrange(x, '(b f) (h w) c -> b c f h w', b = b, h = h, w = w)
        else:
            x = rearrange(x, 'b (h w) c -> b c h w', h = h, w = w)

        if not enable_time:
            return x

        x = rearrange(x, 'b c f h w -> (b h w) f c')

        x = self.temporal_attn(x) + x

        x = rearrange(x, '(b h w) f c -> b c f h w', w = w, h = h)

        return x

# resnet block

class Block(nn.Module):
    def __init__(
        self,
        dim,
        dim_out,
        kernel_size = 3,
        temporal_kernel_size = None,
        groups = 8
    ):
        super().__init__()
        self.project = PseudoConv3d(dim, dim_out, 3)
        self.norm = nn.GroupNorm(groups, dim_out)
        self.act = nn.SiLU()

    def forward(
        self,
        x,
        scale_shift = None,
        enable_time = False
    ):
        x = self.project(x, enable_time = enable_time)
        x = self.norm(x)

        if exists(scale_shift):
            scale, shift = scale_shift
            x = x * (scale + 1) + shift

        return self.act(x)

class ResnetBlock(nn.Module):
    def __init__(
        self,
        dim,
        dim_out,
        *,
        timestep_cond_dim = None,
        groups = 8
    ):
        super().__init__()

        self.timestep_mlp = None

        if exists(timestep_cond_dim):
            self.timestep_mlp = nn.Sequential(
                nn.SiLU(),
                nn.Linear(timestep_cond_dim, dim_out * 2)
            )

        self.block1 = Block(dim, dim_out, groups = groups)
        self.block2 = Block(dim_out, dim_out, groups = groups)
        self.res_conv = PseudoConv3d(dim, dim_out, 1) if dim != dim_out else nn.Identity()

    def forward(
        self,
        x,
        timestep_emb = None,
        enable_time = True
    ):
        assert not (exists(timestep_emb) ^ exists(self.timestep_mlp))

        scale_shift = None

        if exists(self.timestep_mlp) and exists(timestep_emb):
            time_emb = self.timestep_mlp(timestep_emb)
            to_einsum_eq = 'b c 1 1 1' if x.ndim == 5 else 'b c 1 1'
            time_emb = rearrange(time_emb, f'b c -> {to_einsum_eq}')
            scale_shift = time_emb.chunk(2, dim = 1)

        h = self.block1(x, scale_shift = scale_shift, enable_time = enable_time)

        h = self.block2(h, enable_time = enable_time)

        return h + self.res_conv(x)

# pixelshuffle upsamples and downsamples
# where time dimension can be configured

class Downsample(nn.Module):
    def __init__(
        self,
        dim,
        downsample_space = True,
        downsample_time = False
    ):
        super().__init__()
        assert downsample_space or downsample_time

        self.down_space = nn.Sequential(
            Rearrange('b c (h p1) (w p2) -> b (c p1 p2) h w', p1 = 2, p2 = 2),
            nn.Conv2d(dim * 4, dim, 1, bias = False)
        ) if downsample_space else None

        self.down_time = nn.Sequential(
            Rearrange('b c (f p) h w -> b (c p) f h w', p = 2),
            nn.Conv3d(dim * 2, dim, 1, bias = False)
        ) if downsample_time else None

    def forward(
        self,
        x,
        enable_time = True
    ):
        is_video = x.ndim == 5

        if is_video:
            x = rearrange(x, 'b c f h w -> b f c h w')
            x, ps = pack([x], '* c h w')

        if exists(self.down_space):
            x = self.down_space(x)

        if is_video:
            x, = unpack(x, ps, '* c h w')
            x = rearrange(x, 'b f c h w -> b c f h w')

        if not is_video or not exists(self.down_time) or not enable_time:
            return x

        x = self.down_time(x)

        return x

class Upsample(nn.Module):
    def __init__(
        self,
        dim,
        upsample_space = True,
        upsample_time = False
    ):
        super().__init__()
        assert upsample_space or upsample_time

        self.up_space = nn.Sequential(
            nn.Conv2d(dim, dim * 4, 1, bias = False),
            Rearrange('b (c p1 p2) h w -> b c (h p1) (w p2)', p1 = 2, p2 = 2)
        ) if upsample_space else None

        self.up_time = nn.Sequential(
            nn.Conv3d(dim, dim * 2, 1, bias = False),
            Rearrange('b (c p) f h w -> b c (f p) h w', p = 2)
        ) if upsample_time else None

    def forward(
        self,
        x,
        enable_time = True
    ):
        is_video = x.ndim == 5

        if is_video:
            x = rearrange(x, 'b c f h w -> b f c h w')
            x, ps = pack([x], '* c h w')

        if exists(self.up_space):
            x = self.up_space(x)

        if is_video:
            x, = unpack(x, ps, '* c h w')
            x = rearrange(x, 'b f c h w -> b c f h w')

        if not is_video or not exists(self.up_time) or not enable_time:
            return x

        x = self.up_time(x)

        return x

# space time factorized 3d unet

class SpaceTimeUnet(nn.Module):
    def __init__(
        self,
        *,
        dim,
        channels = 3,
        dim_mult = (1, 2, 4, 8),
        self_attns = (False, False, False, True),
        temporal_compression = (False, True, True, True),
        attn_dim_head = 64,
        attn_heads = 8,
        condition_on_timestep = True
    ):
        super().__init__()
        assert len(dim_mult) == len(self_attns) == len(temporal_compression)
        num_layers = len(dim_mult)

        dims = [dim, *map(lambda mult: mult * dim, dim_mult)]
        dim_in_out = zip(dims[:-1], dims[1:])

        # timestep conditioning for DDPM, not to be confused with the time dimension of the video

        self.to_timestep_cond = None
        timestep_cond_dim = (dim * 4) if condition_on_timestep else None

        if condition_on_timestep:
            self.to_timestep_cond = nn.Sequential(
                SinusoidalPosEmb(dim),
                nn.Linear(dim, timestep_cond_dim),
                nn.SiLU()
            )

        # layers

        self.downs = mlist([])
        self.ups = mlist([])

        attn_kwargs = dict(
            dim_head = attn_dim_head,
            heads = attn_heads
        )

        mid_dim = dims[-1]

        self.mid_block1 = ResnetBlock(mid_dim, mid_dim, timestep_cond_dim = timestep_cond_dim)
        self.mid_attn = SpatioTemporalAttention(dim = mid_dim)
        self.mid_block2 = ResnetBlock(mid_dim, mid_dim, timestep_cond_dim = timestep_cond_dim)

        for _, self_attend, (dim_in, dim_out), compress_time in zip(range(num_layers), self_attns, dim_in_out, temporal_compression):

            self.downs.append(mlist([
                ResnetBlock(dim_in, dim_out, timestep_cond_dim = timestep_cond_dim),
                ResnetBlock(dim_out, dim_out),
                SpatioTemporalAttention(dim = dim_out, **attn_kwargs) if self_attend else None,
                Downsample(dim_out, downsample_time = compress_time)
            ]))

            self.ups.append(mlist([
                ResnetBlock(dim_out * 2, dim_in, timestep_cond_dim = timestep_cond_dim),
                ResnetBlock(dim_in, dim_in),
                SpatioTemporalAttention(dim = dim_in, **attn_kwargs) if self_attend else None,
                Upsample(dim_out, upsample_time = compress_time)
                
            ]))

        self.skip_scale = 2 ** -0.5 # paper shows faster convergence

        self.conv_in = PseudoConv3d(dim = channels, dim_out = dim, kernel_size = 7, temporal_kernel_size = 3)
        self.conv_out = PseudoConv3d(dim = dim, dim_out = channels, kernel_size = 3, temporal_kernel_size = 3)

    def forward(
        self,
        x,
        timestep = None,
        enable_time = True
    ):
        assert not (exists(self.to_timestep_cond) ^ exists(timestep))

        t = self.to_timestep_cond(rearrange(timestep, '... -> (...)')) if exists(timestep) else None

        x = self.conv_in(x, enable_time = enable_time)

        hiddens = []

        for block1, block2, maybe_attention, downsample in self.downs:
            x = block1(x, t, enable_time = enable_time)
            x = block2(x, enable_time = enable_time)

            if exists(maybe_attention):
                x = maybe_attention(x, enable_time = enable_time)

            hiddens.append(x.clone())

            x = downsample(x, enable_time = enable_time)

        x = self.mid_block1(x, t, enable_time = enable_time)
        x = self.mid_attn(x, enable_time = enable_time)
        x = self.mid_block2(x, t, enable_time = enable_time)

        for block1, block2, maybe_attention, upsample in reversed(self.ups):
            x = upsample(x, enable_time = enable_time)
            x = torch.cat((hiddens.pop() * self.skip_scale, x), dim = 1)

            x = block1(x, t, enable_time = enable_time)
            x = block2(x, enable_time = enable_time)

            if exists(maybe_attention):
                x = maybe_attention(x, enable_time = enable_time)

        x = self.conv_out(x, enable_time = enable_time)
        return x
