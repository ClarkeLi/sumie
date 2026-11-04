# 文档索引

这些文档目前是中文。安装和日常使用请看根目录的 [README](../README.md)（中/英/日/韩/德）；想先了解整体设计，读技术白皮书：[中文](whitepaper.zh-CN.md) · [English](whitepaper.en.md)。

## 文档地图

| 文档 | 内容 | 什么时候读 |
|---|---|---|
| [`whitepaper.zh-CN.md`](whitepaper.zh-CN.md) / [`whitepaper.en.md`](whitepaper.en.md) | 技术白皮书：目标、架构、人体模型、时间预算、验证、安全 | 想理解整体设计 |
| [`usage.md`](usage.md) | 命令行客户端、渲染器参数、配置与安全、排错 | 想用命令行画图、调参数 |
| [`studio.md`](studio.md) | 8765 工作台、8770 绘制服务、粘贴/风格/选区/预览/作业/恢复 | 用页面画图、排查作业失败 |
| [`architecture.md`](architecture.md) | 组件、线程、坐标系、画笔渲染链路、时间预算分层 | 改 C# 插件、坐标、笔宽之前 |
| [`api-reference.md`](api-reference.md) | 全部 HTTP / WebSocket 端点、请求字段、错误码 | 写客户端、加端点 |
| [`artist-renderer.md`](artist-renderer.md) | `dg_artist.py` / `anime_study.py` 的算法、节奏、规划进度、CPU 占用 | 改画风、笔画规划、时间节奏 |
| [`anime-human.md`](anime-human.md) | `anime_human` 风格：压感模型、真人节奏、油漆桶离线模型、原生笔 | 改手绘风格、压感、倒桶 |
| [`verification.md`](verification.md) | 不依赖视觉模型的数值验证流程 | 每次改动画质后 |
| [`development.md`](development.md) | 构建、改动生效矩阵、排错、代码约定 | 贡献代码 |

## 5 分钟上手（开发）

```bash
python tools/setup.py                 # 创建 .venv，装依赖
python -m unittest discover -s tests  # 离线测试，不需要游戏

# 只看计划，不碰画布
python client/dg_artist.py examples/sample.png --style anime_study --time 120 --dry-run \
  --plan-out .cache/check.json --preview .cache/check.png
```

## 约定

- 游戏坐标系、笔画格式和时间预算见 [`architecture.md`](architecture.md)。
- 画质只用数值指标验证，见 [`verification.md`](verification.md)。
- 所有生成物（`.venv/`、`.cache/`、`dist/`、`models/`）都在仓库目录内，并已被 `.gitignore` 忽略。
