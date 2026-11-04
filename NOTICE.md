# Third-party notices

Sumie is MIT licensed (see `LICENSE`). It uses the following third-party
software. None of the game's own files are included in this repository.

## Downloaded at install time (not stored in this repository)

| Component | Use | License | Source |
|---|---|---|---|
| BepInEx 6 (Unity.Mono, build 790) | plugin loader, installed into the game folder by `tools/install.py` | LGPL-2.1 | https://github.com/BepInEx/BepInEx |
| HarmonyX (shipped inside BepInEx) | runtime patching | MIT | https://github.com/BepInEx/HarmonyX |
| Anime2Sketch weights `netG.pth` (`setup.py --ai`) | optional AI line art | MIT | https://github.com/Mukosame/Anime2Sketch |
| anime-segmentation `isnetis.onnx` (`setup.py --ai`) | optional character segmentation | Apache-2.0 | https://github.com/SkyTNT/anime-segmentation |

Model weights are mirrored on Hugging Face (`public-data/Anime2Sketch`,
`skytnt/anime-seg`); the installer verifies SHA-256 checksums before using them.

## Data used for calibration

`client/calibration/quickdraw_motor.json` holds summary statistics (stroke
duration against length, pauses, speed profile) computed from the raw Google
Quick, Draw! dataset by `tools/calibrate_motor.py`. The dataset is made available
by Google, Inc. under the Creative Commons Attribution 4.0 International license
(https://github.com/googlecreativelab/quickdraw-dataset). The raw drawings are
not part of this repository.

## Derived code

`client/anime2sketch_net.py` re-implements the Anime2Sketch generator network
(U-Net) so that the published `netG.pth` checkpoint can be loaded without the
original repository. Anime2Sketch is Copyright (c) 2021 Xiaoyu Xiang, MIT.

## Python packages (installed from PyPI by `tools/setup.py`)

numpy (BSD-3), opencv-python (Apache-2.0), Pillow (HPND), and, with `--ai`,
PyTorch (BSD-3) and ONNX Runtime (MIT).

## Trademarks

"Draw & Guess" belongs to its respective owners. This project is an unofficial
fan tool and is not affiliated with or endorsed by them.
