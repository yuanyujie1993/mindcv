"""
MindSpore implementation of `ConViT`.
Refer to ConViT: Improving Vision Transformers with Soft Convolutional Inductive Biases
"""

import numpy as np

import mindspore as ms
import mindspore.nn as nn
import mindspore.ops as ops
from mindspore.ops import constexpr
from mindspore import Parameter, Tensor
import mindspore.common.initializer as init

from .layers.identity import Identity
from .layers.patch_embed import PatchEmbed
from .layers.drop_path import DropPath
from .layers.mlp import Mlp
from .utils import load_pretrained
from .registry import register_model


__all__ = [
    "ConViT",
    "convit_tiny",
    "convit_tiny_plus",
    "convit_small",
    "convit_small_plus",
    "convit_base",
    "convit_base_plus"
]


def _cfg(url='', **kwargs):
    return {
        'url': url,
        'num_classes': 1000,
        'input_size': (3, 224, 224),
        'first_conv': 'patch_embed.proj', 'classifier': 'classifier',
        **kwargs
    }


default_cfgs = {
    'convit_tiny': _cfg(url=''),
    'convit_tiny_plus': _cfg(url=''),
    'convit_small': _cfg(url=''),
    'convit_small_plus': _cfg(url=''),
    'convit_base': _cfg(url=''),
    'convit_base_plus': _cfg(url='')
}


class GPSA(nn.Cell):

    def __init__(self, 
                 dim: int, 
                 num_heads: int, 
                 qkv_bias: bool = False, 
                 attn_drop: float = 0., 
                 proj_drop: float = 0.,
                 locality_strength: float = 1.) -> None:
        super().__init__()

        self.dim = dim
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim ** -0.5

        self.qk = nn.Dense(in_channels=dim, out_channels=dim * 2, has_bias=qkv_bias)
        self.v = nn.Dense(in_channels=dim, out_channels=dim, has_bias=qkv_bias)

        self.attn_drop = nn.Dropout(keep_prob=1.0 - attn_drop)
        self.proj = nn.Dense(in_channels=dim, out_channels=dim)
        self.pos_proj = nn.Dense(in_channels=3, out_channels=num_heads)
        self.proj_drop = nn.Dropout(keep_prob=1.0 - proj_drop)
        self.gating_param = Parameter(ops.ones((num_heads), ms.float32))
        self.rel_indices = self.get_rel_indices()
        self.local_init(locality_strength=locality_strength)

    def construct(self, x: Tensor) -> Tensor:
        B, N, C = x.shape
        attn = self.get_attention(x)
        v = self.v(x)
        v = ops.reshape(v, (B, N, self.num_heads, C // self.num_heads))
        v = ops.transpose(v, (0, 2, 1, 3))
        x = ops.transpose(ops.BatchMatMul()(attn, v), (0, 2, 1, 3))
        x = ops.reshape(x, (B, N, C))
        x = self.proj(x)
        x = self.proj_drop(x)
        return x

    def get_attention(self, x: Tensor) -> Tensor:
        B, N, C = x.shape
        qk = self.qk(x)
        qk = ops.reshape(qk, (B, N, 2, self.num_heads, C // self.num_heads))
        qk = ops.transpose(qk, (2, 0, 3, 1, 4))
        q, k = qk[0], qk[1]
        pos_score = ops.tile(self.rel_indices, (B, 1, 1, 1))
        pos_score = self.pos_proj(pos_score)
        pos_score = ops.transpose(pos_score, (0, 3, 1, 2))
        patch_score = ops.BatchMatMul(transpose_b=True)(q, k) * self.scale
        patch_score = nn.Softmax(axis=-1)(patch_score)
        pos_score = nn.Softmax(axis=-1)(pos_score)

        gating = self.gating_param.view((1, -1, 1, 1))
        attn = (1.-ops.Sigmoid()(gating)) * patch_score + ops.Sigmoid()(gating) * pos_score
        attn /= ops.expand_dims(attn.sum(axis=-1), -1)
        attn = self.attn_drop(attn)
        return attn

    def local_init(self, locality_strength: float) -> None:
        self.v.weight.set_data(ops.eye(self.dim, self.dim, ms.float32), slice_shape=True)
        locality_distance = 1
        kernel_size = int(self.num_heads**.5)
        center = (kernel_size - 1) / 2 if kernel_size % 2 == 0 else kernel_size // 2

        pos_weight_data = self.pos_proj.weight.data
        for h1 in range(kernel_size):
            for h2 in range(kernel_size):
                position = h1+kernel_size*h2
                pos_weight_data[position,2] = -1
                pos_weight_data[position,1] = 2*(h1-center)*locality_distance
                pos_weight_data[position,0] = 2*(h2-center)*locality_distance
        pos_weight_data = pos_weight_data * locality_strength
        self.pos_proj.weight.set_data(self.pos_proj.weight.data)

    @constexpr
    def get_rel_indices(num_patches: int = 196) -> Tensor:
        img_size = int(num_patches**.5)
        rel_indices = ops.Zeros()((1, num_patches, num_patches, 3), ms.float32)
        ind = ms.numpy.arange(img_size).view(1, -1) - ms.numpy.arange(img_size).view(-1, 1)
        indx = ms.numpy.tile(ind, (img_size, img_size))
        indy_ = ops.repeat_elements(ind, rep=img_size, axis=0)
        indy = ops.repeat_elements(indy_, rep=img_size, axis=1)
        indd = indx**2 + indy**2
        rel_indices[:, :, :, 2] = ops.expand_dims(indd, 0)
        rel_indices[:, :, :, 1] = ops.expand_dims(indy, 0)
        rel_indices[:, :, :, 0] = ops.expand_dims(indx, 0)
        return rel_indices


class MHSA(nn.Cell):

    def __init__(self, 
                 dim: int, 
                 num_heads: int, 
                 qkv_bias: bool = False, 
                 attn_drop: float = 0., 
                 proj_drop: float = 0.) -> None:
        super().__init__()

        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim ** -0.5

        self.qkv = nn.Dense(in_channels=dim, out_channels=dim * 3, has_bias=qkv_bias)
        self.attn_drop = nn.Dropout(keep_prob=1.0 - attn_drop)
        self.proj = nn.Dense(in_channels=dim, out_channels=dim)
        self.proj_drop = nn.Dropout(keep_prob=1.0 - proj_drop)

    def construct(self, x: Tensor) -> Tensor:
        B, N, C = x.shape
        qkv = self.qkv(x)
        qkv = ops.reshape(qkv, (B, N, 3, self.num_heads, C // self.num_heads))
        qkv = ops.transpose(qkv, (2, 0, 3, 1, 4))
        q, k, v = qkv[0], qkv[1], qkv[2]

        attn = ops.BatchMatMul(transpose_b=True)(q, k) * self.scale
        attn = nn.Softmax(axis=-1)(attn)
        attn = self.attn_drop(attn)

        x = ops.transpose(ops.BatchMatMul()(attn, v), (0, 2, 1, 3))
        x = ops.reshape(x, (B, N, C))
        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class Block(nn.Cell):
    """Basic module of ConViT"""

    def __init__(self, 
                 dim: int, 
                 num_heads: int, 
                 mlp_ratio: float, 
                 qkv_bias: bool = False, 
                 drop: float = 0., 
                 attn_drop: float = 0.,
                 drop_path: float = 0., 
                 use_gpsa: bool = True, 
                 **kwargs) -> None:
        super().__init__()

        self.norm1 = nn.LayerNorm((dim,))
        if use_gpsa:
            self.attn = GPSA(dim, num_heads=num_heads, qkv_bias=qkv_bias,
                             attn_drop=attn_drop, proj_drop=drop, **kwargs)
        else:
            self.attn = MHSA(dim, num_heads=num_heads, qkv_bias=qkv_bias, 
                             attn_drop=attn_drop, proj_drop=drop, **kwargs)
        self.drop_path = DropPath(drop_path) if drop_path > 0. else Identity()
        self.norm2 = nn.LayerNorm((dim,))
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, drop=drop)

    def construct(self, x: Tensor) -> Tensor:
        x = x + self.drop_path(self.attn(self.norm1(x)))
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x


class ConViT(nn.Cell):
    r"""ConViT model class, based on
    '"Improving Vision Transformers with Soft Convolutional Inductive Biases"
    <https://arxiv.org/pdf/2103.10697.pdf>'

    Args:
        in_channels (int): number the channels of the input. Default: 3.
        num_classes (int) : number of classification classes. Default: 1000.
        image_size (int) : images input size. Default: 224.
        patch_size (int) : image patch size. Default: 16.
        embed_dim (int) : embedding dimension in all head. Default: 48.
        num_heads (int) : number of heads. Default: 12.
        drop_rate (float) : dropout rate. Default: 0.
        drop_path_rate (float) : drop path rate. Default: 0.1.
        depth (int) : model block depth. Default: 12.
        mlp_ratio (float) : ratio of hidden features in Mlp. Default: 4.
        qkv_bias (bool) : have bias in qkv layers or not. Default: False.
        attn_drop_rate (float) : attention layers dropout rate. Default: 0.
        locality_strength (float) : determines how focused each head is around its attention center. Default: 1.
        local_up_to_layer (int) : number of GPSA layers. Default: 10.
    """

    def __init__(self, 
                 in_channels: int = 3,
                 num_classes: int = 1000,
                 image_size: int = 224,
                 patch_size: int = 16,
                 embed_dim: int = 48, 
                 num_heads: int = 12, 
                 drop_rate: float = 0., 
                 drop_path_rate: float = 0.1, 
                 depth: int = 12,
                 mlp_ratio: float = 4., 
                 qkv_bias: bool = False, 
                 attn_drop_rate: float = 0.,
                 locality_strength: float = 1., 
                 local_up_to_layer: int = 10, 
                 use_pos_embed: bool = True) -> None:
        super().__init__()

        self.local_up_to_layer = local_up_to_layer
        self.use_pos_embed = use_pos_embed

        self.patch_embed = PatchEmbed(
            image_size=image_size, patch_size=patch_size, in_chans=in_channels, embed_dim=embed_dim)
        self.num_patches = self.patch_embed.num_patches

        self.cls_token = Parameter(ops.Zeros()((1, 1, embed_dim), ms.float32))
        self.pos_drop = nn.Dropout(keep_prob=1.0 - drop_rate)

        if self.use_pos_embed:
            self.pos_embed = Parameter(ops.Zeros()((1, self.num_patches, embed_dim), ms.float32))
            self.pos_embed.set_data(init.initializer(init.TruncatedNormal(sigma=.02), self.pos_embed.data.shape))

        dpr = [x.item() for x in np.linspace(0, drop_path_rate, depth)]
        self.blocks = nn.CellList([
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, 
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], 
                use_gpsa=True, locality_strength=locality_strength)
            if i<local_up_to_layer else
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, 
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], 
                use_gpsa=False)
            for i in range(depth)])
        self.norm = nn.LayerNorm((embed_dim,))
        
        self.classifier = nn.Dense(in_channels=embed_dim, out_channels=num_classes) if num_classes > 0 else Identity()
        self.cls_token.set_data(init.initializer(init.TruncatedNormal(sigma=.02), self.cls_token.data.shape))
        self._initialize_weights()

    def _initialize_weights(self) -> None:
        for _, cell in self.cells_and_names():
            if isinstance(cell, nn.Dense):
                cell.weight.set_data(init.initializer(init.TruncatedNormal(sigma=.02), cell.weight.data.shape))
                if cell.bias is not None:
                    cell.bias.set_data(init.initializer(init.Constant(0), cell.bias.shape))
            elif isinstance(cell, nn.LayerNorm):
                cell.gamma.set_data(init.initializer(init.Constant(1), cell.gamma.shape))
                cell.beta.set_data(init.initializer(init.Constant(0), cell.beta.shape))

    def forward_features(self, x: Tensor) -> Tensor:
        x = self.patch_embed(x)
        if self.use_pos_embed:
            x = x + self.pos_embed
        x = self.pos_drop(x)
        cls_tokens = ops.tile(self.cls_token, (x.shape[0], 1, 1))
        for u,blk in enumerate(self.blocks):
            if u == self.local_up_to_layer :
                x = ops.Concat(1)((cls_tokens, x))
            x = blk(x)
        x = self.norm(x)
        return x[:, 0]

    def forward_head(self, x: Tensor) -> Tensor:
        x = self.classifier(x)
        return x

    def construct(self, x: Tensor) -> Tensor:
        x = self.forward_features(x)
        x = self.forward_head(x)
        return x


@register_model
def convit_tiny(pretrained: bool = False, num_classes: int = 1000, in_channels: int = 3, **kwargs) -> ConViT:
    """Get ConViT tiny model
    Refer to the base class "models.ConViT" for more details.
    """
    default_cfg = default_cfgs['convit_tiny']
    model = ConViT(in_channels=in_channels, num_classes=num_classes,
                   num_heads=4, embed_dim=192, **kwargs)

    if pretrained:
        load_pretrained(model, default_cfg, num_classes=num_classes, in_channels=in_channels)

    return model


@register_model
def convit_tiny_plus(pretrained: bool = False, num_classes: int = 1000, in_channels: int = 3, **kwargs) -> ConViT:
    """Get ConViT tiny+ model
    Refer to the base class "models.ConViT" for more details.
    """
    default_cfg = default_cfgs['convit_tiny_plus']
    model = ConViT(in_channels=in_channels, num_classes=num_classes,
                   num_heads=4, embed_dim=256, **kwargs)

    if pretrained:
        load_pretrained(model, default_cfg, num_classes=num_classes, in_channels=in_channels)

    return model


@register_model
def convit_small(pretrained: bool = False, num_classes: int = 1000, in_channels: int = 3, **kwargs) -> ConViT:
    """Get ConViT small model
    Refer to the base class "models.ConViT" for more details.
    """
    default_cfg = default_cfgs['convit_small']
    model = ConViT(in_channels=in_channels, num_classes=num_classes,
                   num_heads=9, embed_dim=432, **kwargs)

    if pretrained:
        load_pretrained(model, default_cfg, num_classes=num_classes, in_channels=in_channels)

    return model


@register_model
def convit_small_plus(pretrained: bool = False, num_classes: int = 1000, in_channels: int = 3, **kwargs) -> ConViT:
    """Get ConViT small+ model
    Refer to the base class "models.ConViT" for more details.
    """
    default_cfg = default_cfgs['convit_small_plus']
    model = ConViT(in_channels=in_channels, num_classes=num_classes,
                   num_heads=9, embed_dim=576, **kwargs)

    if pretrained:
        load_pretrained(model, default_cfg, num_classes=num_classes, in_channels=in_channels)

    return model


@register_model
def convit_base(pretrained: bool = False, num_classes: int = 1000, in_channels: int = 3, **kwargs) -> ConViT:
    """Get ConViT base model
    Refer to the base class "models.ConViT" for more details.
    """
    default_cfg = default_cfgs['convit_base']
    model = ConViT(in_channels=in_channels, num_classes=num_classes,
                   num_heads=16, embed_dim=768, **kwargs)

    if pretrained:
        load_pretrained(model, default_cfg, num_classes=num_classes, in_channels=in_channels)

    return model


@register_model
def convit_base_plus(pretrained: bool = False, num_classes: int = 1000, in_channels: int = 3, **kwargs) -> ConViT:
    """Get ConViT base+ model
    Refer to the base class "models.ConViT" for more details.
    """
    default_cfg = default_cfgs['convit_base_plus']
    model = ConViT(in_channels=in_channels, num_classes=num_classes,
                   num_heads=16, embed_dim=1024, **kwargs)

    if pretrained:
        load_pretrained(model, default_cfg, num_classes=num_classes, in_channels=in_channels)

    return model
