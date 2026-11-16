# 第三方代码来源

本目录下的两个文件 vendor 自开源项目 **L0NE-6/Trae-AutoCheckin**：

- 上游仓库：https://github.com/L0NE-6/Trae-AutoCheckin
- 上游版本：v1.0.9（2026-11-04）
- 上游协议：MIT
- 上游作者：L0NE-6

| 文件 | 用途 |
|---|---|
| `trae_checkin.py` | 签到 / 续期 / 设备证明（ECDSA P-256）/ 限流处理 |
| `trae_get_token.py` | 从客户端 `storage.json` 提取 refreshToken、导出设备密钥 |

## 本仓库所做的修改

1. `trae_checkin.py` 的 `decrypt_storage_value()` 增加了**纯标准库 AES-128-CBC 解密回退**：
   上游在未安装 `cryptography` 时会直接退出，本仓库改为优先用 `cryptography`（若已安装），
   缺失时走纯 Python 实现，从而保持与 `signin.py` / `qoder_signin.py` 一致的"零第三方依赖"。
2. 其余逻辑未改动。本仓库新增的 `trae_signin.py` 是独立封装（一行 JSON 输出 + 日志 + 计划任务）。

## MIT License（上游）

Copyright (c) 2026 L0NE-6

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

> 签到接口与设备证明参数均逆向自 Trae 桌面端，仅供个人自动化使用。
> 使用风险自负，请遵守 Trae 的服务条款。
