"""Minimal Anime2Sketch generator (U-Net), compatible with netG.pth.

The checkpoint stores a DataParallel wrapper (``module.`` prefix) and uses
InstanceNorm without affine parameters, so the state dict has only conv
weights/biases.
"""

from __future__ import annotations

import torch
import torch.nn as nn


class UnetSkipConnectionBlock(nn.Module):
    def __init__(self, outer_nc, inner_nc, input_nc=None, submodule=None,
                 outermost=False, innermost=False, norm_layer=nn.InstanceNorm2d,
                 use_dropout=False):
        super().__init__()
        self.outermost = outermost
        use_bias = norm_layer is nn.InstanceNorm2d
        if input_nc is None:
            input_nc = outer_nc

        downconv = nn.Conv2d(input_nc, inner_nc, kernel_size=4, stride=2,
                             padding=1, bias=use_bias)
        downrelu = nn.LeakyReLU(0.2, inplace=True)
        downnorm = norm_layer(inner_nc)
        uprelu = nn.ReLU(inplace=True)
        upnorm = norm_layer(outer_nc)

        if outermost:
            upconv = nn.ConvTranspose2d(inner_nc * 2, outer_nc, kernel_size=4,
                                        stride=2, padding=1)
            model = [downconv, submodule, uprelu, upconv, nn.Tanh()]
        elif innermost:
            upconv = nn.ConvTranspose2d(inner_nc, outer_nc, kernel_size=4,
                                        stride=2, padding=1, bias=use_bias)
            model = [downrelu, downconv, uprelu, upconv, upnorm]
        else:
            upconv = nn.ConvTranspose2d(inner_nc * 2, outer_nc, kernel_size=4,
                                        stride=2, padding=1, bias=use_bias)
            model = [downrelu, downconv, downnorm, submodule, uprelu, upconv, upnorm]
            if use_dropout:
                model.append(nn.Dropout(0.5))

        self.model = nn.Sequential(*model)

    def forward(self, x):
        if self.outermost:
            return self.model(x)
        return torch.cat([x, self.model(x)], 1)


class UnetGenerator(nn.Module):
    def __init__(self, input_nc=3, output_nc=1, num_downs=8, ngf=64,
                 norm_layer=nn.InstanceNorm2d, use_dropout=False):
        super().__init__()
        block = UnetSkipConnectionBlock(ngf * 8, ngf * 8, input_nc=None,
                                        submodule=None, innermost=True,
                                        norm_layer=norm_layer)
        for _ in range(num_downs - 5):
            block = UnetSkipConnectionBlock(ngf * 8, ngf * 8, input_nc=None,
                                            submodule=block, norm_layer=norm_layer,
                                            use_dropout=use_dropout)
        block = UnetSkipConnectionBlock(ngf * 4, ngf * 8, input_nc=None,
                                        submodule=block, norm_layer=norm_layer)
        block = UnetSkipConnectionBlock(ngf * 2, ngf * 4, input_nc=None,
                                        submodule=block, norm_layer=norm_layer)
        block = UnetSkipConnectionBlock(ngf, ngf * 2, input_nc=None,
                                        submodule=block, norm_layer=norm_layer)
        block = UnetSkipConnectionBlock(output_nc, ngf, input_nc=input_nc,
                                        submodule=block, outermost=True,
                                        norm_layer=norm_layer)
        self.model = block

    def forward(self, x):
        return self.model(x)


def load_checkpoint(path, device="cpu"):
    state = torch.load(path, map_location="cpu", weights_only=True)
    if isinstance(state, dict) and "state_dict" in state:
        state = state["state_dict"]
    cleaned = {}
    for key, value in state.items():
        if key.startswith("module."):
            key = key[len("module."):]
        cleaned[key] = value
    model = UnetGenerator(3, 1, 8, 64, norm_layer=nn.InstanceNorm2d)
    model.load_state_dict(cleaned, strict=True)
    model.eval().to(device)
    return model
