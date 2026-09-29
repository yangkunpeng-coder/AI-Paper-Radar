# 论文自动检索分析

**AI Paper Auto Retrieval & Analysis**

一款用于自动检索、管理和 AI 解读 arXiv 论文的 Windows 桌面应用。

## 功能

- 自动检索和管理 arXiv 论文
- 支持 **具身智能 / 智能体 / 大语言模型** 三个研究领域
- DeepSeek AI 论文快速解读
- 本地 SQLite 论文库
- 论文收藏、时间筛选、研究方向筛选与排序
- 作者单位补全
- Excel / Markdown 导出
- 自动同步与本地持久化

## 下载

当前正式版本：

**v1.0.0 — Windows 10 / 11 64 位**

请前往 GitHub 仓库的 **Releases** 页面下载：

- `AI-Paper-Radar-v1.0.0.exe`
- `AI-Paper-Radar-v1.0.0-demo.mp4`
- `SHA256SUMS.txt`

[前往 Releases](../../releases/latest)

> 软件为单文件 Windows 可执行程序，无需安装 Python 或 Flet。

## 使用方法

1. 下载 `AI-Paper-Radar-v1.0.0.exe`
2. 双击运行
3. 点击“同步”获取最新论文
4. 使用时间范围、研究方向和排序条件筛选论文
5. 点击论文查看详细信息和原始摘要
6. 如需 AI 快速解读，请在设置中填写自己的 DeepSeek API Key
7. 可按需导出 Excel 或 Markdown

## DeepSeek API Key

DeepSeek 功能需要用户自行提供 API Key。

API Key 由软件保存在本机，不随软件发行包提供。

如果不配置 DeepSeek API Key，论文检索、浏览、收藏、筛选、排序和导出等本地功能仍可正常使用。

## 软件演示

`v1.0.0` Release 中提供操作录屏：

`AI-Paper-Radar-v1.0.0-demo.mp4`

演示内容包括：

- 软件启动
- 三个研究领域切换
- 论文筛选与排序
- 论文详情查看
- DeepSeek 快速解读
- 收藏
- arXiv 同步
- Excel / Markdown 导出

## 系统要求

- Windows 10 或 Windows 11
- 64 位系统
- 论文同步需要互联网连接
- DeepSeek 分析需要互联网连接及用户自己的 DeepSeek API Key

## 文件完整性校验

`AI-Paper-Radar-v1.0.0.exe`

SHA256：

```text
04113bc119848f4706069defb46cac8b852b978c882be59c2d260f449e6d55be
```

也可以下载 Release 中的 `SHA256SUMS.txt` 进行校验。

Windows CMD 校验命令：

```cmd
certutil -hashfile AI-Paper-Radar-v1.0.0.exe SHA256
```

输出应与上面的 SHA256 一致。

## Windows 安全提示

当前版本暂未进行 Windows 代码签名。

首次运行时，Microsoft Defender SmartScreen 可能显示“未知发布者”或安全提示。这是未签名的新 Windows 桌面应用常见现象。

请仅从本项目官方 GitHub Release 页面下载软件，并可使用上述 SHA256 校验文件完整性。

## 数据说明

论文数据库、收藏、设置及分析结果均保存在用户本机。

软件发行包不包含开发者的 DeepSeek API Key，也不会预置用户私有数据。

## 版本

当前版本：**v1.0.0**

这是 AI Paper Auto Retrieval & Analysis 的首个 Windows 正式公开版本。
