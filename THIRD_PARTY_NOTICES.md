# 第三方来源与许可

本地改动于 2026-10-08 整理为公开版本；第三方资源保留各自许可证和版权声明。

## OASX 图标与布局

`static/assets/oasx.png` 及面板的 OASX 风格视觉布局参考来自 [runhey/OASX](https://github.com/runhey/OASX)，并沿用 [AzurTian/OASX](https://github.com/AzurTian/OASX) 分支中的前端资源。配套定制桌面前端发布于 [luchi5/OASX](https://github.com/luchi5/OASX)。网页图标与 OASX 的 `assets/images/Icon-app.png` 内容相同。

OASX 采用 GNU General Public License version 3。保留的许可证文本位于 `static/assets/OASX-LICENSE.txt`；本项目根目录 `LICENSE` 同样为 GPLv3。原作者及贡献者保留各自版权。

## Lato

文件：`static/assets/Lato-Regular.ttf`。

Copyright (c) 2010–2014 by tyPoland Lukasz Dziedzic. Reserved Font Name: Lato。

许可：SIL Open Font License 1.1。完整原始版权声明及许可见 `static/assets/Lato-OFL.txt`。许可来源：[Google Fonts 的 Lato OFL.txt](https://github.com/google/fonts/blob/main/ofl/lato/OFL.txt)；字体项目：[Lato](https://www.latofonts.com/)。该字体按 OFL 许可分发。

## Material Icons

文件：`static/assets/MaterialIcons-Regular.otf`。

来源：[Google Material Design Icons](https://github.com/google/material-design-icons)。许可：Apache License 2.0，完整文本见 `static/assets/MaterialIcons-LICENSE.txt`。许可来源：[官方 LICENSE](https://github.com/google/material-design-icons/blob/master/LICENSE)。图标字体保留其独立 Apache 2.0 许可。

## OAS 统计测试夹具

`tests/fixtures/log_stats.py` 是配套 [luchi5/OnmyojiAutoScript 的 luchi 分支](https://github.com/luchi5/OnmyojiAutoScript/tree/luchi) 中 `module/server/log_stats.py` 的源码副本，用于临时目录中的离线统计测试。后端源项目为 [runhey/OnmyojiAutoScript](https://github.com/runhey/OnmyojiAutoScript)。夹具按 GPLv3 分发；它不包含运行数据、实际日志或账号配置。

## Python 依赖

`requirements.lock.txt` 只声明依赖版本；Python 及安装后的依赖包不随仓库分发。安装时请保留相应包的许可证。
