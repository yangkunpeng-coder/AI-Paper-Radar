# 论文自动检索分析

<div align="center">

**AI Paper Auto Retrieval & Analysis**

面向研究人员的 Windows 桌面论文工具：  
**自动检索 arXiv · 本地管理 · DeepSeek 快速解读 · 收藏筛选 · Excel / Markdown 导出**

<br>

[**⬇ 下载 Windows v1.0.0**](https://github.com/yangkunpeng-coder/AI-Paper-Radar/releases/tag/v1.0.0)
&nbsp;&nbsp;·&nbsp;&nbsp;
[**▶ 查看软件演示**](https://github.com/yangkunpeng-coder/AI-Paper-Radar/releases/download/v1.0.0/AI-Paper-Radar-v1.0.0-demo.mp4)
&nbsp;&nbsp;·&nbsp;&nbsp;
[**📦 所有 Releases**](https://github.com/yangkunpeng-coder/AI-Paper-Radar/releases)

</div>

---

## 一分钟了解

这是一款用于 **自动发现、整理和快速理解 arXiv 论文** 的 Windows 桌面软件。

当前支持三个研究领域：

- **具身智能**
- **智能体**
- **大语言模型**

软件将论文保存在本地 SQLite 数据库中，并可按时间、研究方向、收藏状态等条件筛选；如配置自己的 DeepSeek API Key，还可以直接生成论文快速解读。

> **想先看看软件怎么用？**  
> ▶ [点击查看 v1.0.0 完整操作演示](https://github.com/yangkunpeng-coder/AI-Paper-Radar/releases/download/v1.0.0/AI-Paper-Radar-v1.0.0-demo.mp4)

---

## 软件界面

![论文自动检索分析主界面](assets/screenshot.png)

---

## 核心功能

### 自动检索 arXiv

- 自动获取最新论文
- 支持三个独立研究领域
- 支持时间范围筛选
- 支持研究方向筛选
- 支持相关度 / 最新更新时间排序
- 本地保存同步结果，无需每次重新获取

### DeepSeek 论文快速解读

配置自己的 DeepSeek API Key 后，可以为论文生成结构化快速解读，包括：

- 一句话看懂
- 研究问题
- 方法
- 主要结果
- 主题标签

未配置 DeepSeek API Key 时，论文检索、浏览、收藏、筛选、排序和导出等功能仍可正常使用。

### 本地论文库

- SQLite 本地存储
- 收藏论文
- 作者及单位信息
- 原始摘要
- arXiv / PDF 快速打开
- 本地持久化保存

### 导出

支持将筛选结果导出为：

- **Excel**
- **Markdown**

---

## 下载

### Windows 正式版

当前版本：**v1.0.0**

[**前往 GitHub Release 下载**](https://github.com/yangkunpeng-coder/AI-Paper-Radar/releases/tag/v1.0.0)

Release 中包含：

| 文件 | 用途 |
|---|---|
| `AI-Paper-Radar-v1.0.0.exe` | Windows 可执行程序 |
| `AI-Paper-Radar-v1.0.0-demo.mp4` | 软件完整操作演示 |
| `SHA256SUMS.txt` | EXE 文件完整性校验 |

> Windows 版为单文件可执行程序，普通用户无需安装 Python 或 Flet。

---

## 快速开始

1. 下载 `AI-Paper-Radar-v1.0.0.exe`
2. 双击运行
3. 选择 **具身智能 / 智能体 / 大语言模型**
4. 点击 **同步** 获取论文
5. 使用时间范围、研究方向和排序条件筛选
6. 点击论文查看详细信息
7. 如需 AI 快速解读，在 **设置** 中填写自己的 DeepSeek API Key
8. 按需收藏论文或导出 Excel / Markdown

---

## DeepSeek API Key

DeepSeek 功能需要用户自行提供 API Key。

- 软件发行包**不包含**开发者 API Key
- 用户 API Key 保存在本机
- 不配置 API Key 不影响论文检索、浏览、收藏、筛选和导出

---

## 软件演示

如果不确定软件是否适合自己的工作流，可以先看操作录屏：

### ▶ [查看 AI Paper Radar v1.0.0 操作演示](https://github.com/yangkunpeng-coder/AI-Paper-Radar/releases/download/v1.0.0/AI-Paper-Radar-v1.0.0-demo.mp4)

演示包括：

- 软件启动
- 三个研究领域切换
- arXiv 同步
- 时间 / 研究方向 / 排序筛选
- 论文详情
- DeepSeek 快速解读
- 收藏
- Excel / Markdown 导出

---

## 系统要求

- **Windows 10 / Windows 11**
- **64 位系统**
- 论文同步需要互联网连接
- DeepSeek 分析需要互联网连接及用户自己的 DeepSeek API Key

---

## 文件完整性校验

`AI-Paper-Radar-v1.0.0.exe`

SHA256：

```text
04113bc119848f4706069defb46cac8b852b978c882be59c2d260f449e6d55be
```

Release 中同时提供：

`SHA256SUMS.txt`

Windows CMD 校验：

```cmd
certutil -hashfile AI-Paper-Radar-v1.0.0.exe SHA256
```

输出应与上面的 SHA256 完全一致。

---

## Windows 安全提示

当前 v1.0.0 暂未进行 Windows 代码签名。

因此首次启动时，Microsoft Defender SmartScreen 可能显示 **“未知发布者”** 或相关安全提示。对于尚未进行代码签名的新 Windows 桌面应用，这是常见情况。

建议：

- 仅从本项目 GitHub Releases 页面下载
- 下载后使用 SHA256 校验文件完整性

---

## 数据与隐私

- 论文数据库保存在用户本机
- 收藏、设置和分析结果保存在用户本机
- 软件发行包不会预置用户私有数据
- 软件发行包不会包含开发者 DeepSeek API Key

---

## 当前版本

**v1.0.0**

AI Paper Auto Retrieval & Analysis 的首个 Windows 正式公开版本。

---

<div align="center">

[**下载 v1.0.0**](https://github.com/yangkunpeng-coder/AI-Paper-Radar/releases/tag/v1.0.0)
&nbsp;&nbsp;·&nbsp;&nbsp;
[**查看演示**](https://github.com/yangkunpeng-coder/AI-Paper-Radar/releases/download/v1.0.0/AI-Paper-Radar-v1.0.0-demo.mp4)
&nbsp;&nbsp;·&nbsp;&nbsp;
[**Releases**](https://github.com/yangkunpeng-coder/AI-Paper-Radar/releases)

</div>
