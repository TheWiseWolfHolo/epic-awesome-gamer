<div align="center">

# 🎮 Epic Awesome Gamer
### (LLM Compatible Edition)

<img src="https://img.shields.io/static/v1?message=Python%203.12&color=3776AB&style=for-the-badge&logo=python&label=Build">
<img src="https://img.shields.io/static/v1?message=Gemini%20Pro&color=4285F4&style=for-the-badge&logo=google&label=AI%20Model">
<img src="https://img.shields.io/github/license/10000ge10000/epic-awesome-gamer?style=for-the-badge&color=orange">
<img src="https://img.shields.io/github/actions/workflow/status/10000ge10000/epic-awesome-gamer/ci.yaml?label=Auto%20Claim&style=for-the-badge&color=2ea44f">

<p class="description">
  🍷 <b>优雅、智能、全自动</b>。<br>
  专为 GitHub Actions 打造的 Epic Games Store 免费游戏领取机器人。
</p>

[特性一览](#-核心特性) • [快速部署](#-部署指南-github-actions) • [配置说明](#-配置详解-secrets) • [常见问题](#-常见问题-faq)

</div>

---

## 📖 项目简介

**Epic Awesome Gamer** 是一款基于 Python 的全自动 Epic 游戏领取工具。

本项目基于原作者 [**QIN2DIM/epic-awesome-gamer**](https://github.com/QIN2DIM/epic-awesome-gamer) 进行二次开发与深度重构。在此特别感谢原作者的开源贡献与灵感！

**本修改版的主要改进：**
* 集成了**可配置的 LLM 调用层**：支持 **OpenAI 兼容** & **Gemini 官方（native / OpenAI 兼容）** 三种模式，且**严禁私自改写 base_url**。
* 增强了可观测性：启动 preflight + 非 JSON/HTML/WAF 响应会输出关键信息，排障不再只有 JSONDecodeError。
* 专门针对 **GitHub Actions** 环境优化，无需本地挂机。
* 新增 **即时结账 (Instant Checkout)** 和 **弹窗拦截** 逻辑，修复了无法领取 **特殊游戏** 的问题。

## ✨ 核心特性

| 模块 | 功能描述 |
| :--- | :--- |
| **🤖 AI 强力驱动** | 内置可配置的 LLM 调用层，适配任意中转站/网关 `base_url`，支持 Base64 图片直传，**0 报错**通过 hCaptcha 验证。 |
| **⚡️ 即时结账支持** | 独家支持 **Instant Checkout** 流程。自动识别点击 "Get" 后弹出的支付窗口，不再因为找不到购物车而漏领。 |
| **🛡️ 智能弹窗处理** | 自动识别并处理 **"内容警告 (Content Warning)"** 和年龄限制弹窗，确保脚本不会卡在确认页面。 |
| **📦 全内容收集** | 移除了原版的捆绑包过滤逻辑，无论是普通游戏还是 **Bundles**，所有免费内容一网打尽。 |
| **☁️ 云端自动运行** | 深度适配 GitHub Actions，利用 `uv` 极速管理依赖，每周定时自动执行，零成本守护游戏库。 |

---

## 🚀 部署指南 (GitHub Actions)

这是最推荐的部署方式，完全免费，配置一次即可永久自动运行。

### 1. Fork 仓库
点击页面右上角的 **Fork** 按钮，将本项目克隆到你自己的 GitHub 账号下。

### 2. 配置 Secrets
进入你 Fork 后的仓库，依次点击：
`Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`

添加以下必要变量：

| 变量名 | 必填 | 说明 | 示例 |
| :--- | :---: | :--- | :--- |
| `EPIC_EMAIL` | ✅ | Epic 账号邮箱 (**必须关闭 2FA**) | `myname@email.com` |
| `EPIC_PASSWORD` | ✅ | Epic 账号密码 | `password123` |
| `GEMINI_API_KEY` | ✅ | Gemini 官方 / OpenAI 兼容服务的 API Key | `sk-xxxxxxxx` |

### 3. 可选配置 (Advanced)

| 变量名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `LLM_MODE` | `gemini_native` | 三种模式：`openai` / `gemini_native` / `gemini_openai` |
| `LLM_BASE_URL` | `https://generativelanguage.googleapis.com` | 由你提供的 Base URL；代码**绝不会**自动补 `/gemini`、`/v1` 等站点约定路径（除非你自己写在 base_url 里）。仅在 Gemini 两种模式下按官方规范自动追加 `/v1beta` 或 `/v1beta/openai/` |
| `GEMINI_BASE_URL` | `https://generativelanguage.googleapis.com` | **兼容旧变量**：未设置 `LLM_BASE_URL` 时回退使用；同样不会被代码私自改写 |
| `GEMINI_MODEL` | `gemini-2.5-pro` | **你填什么就用什么**：本项目会把它作为验证码求解的默认模型（并显式注入到 `CHALLENGE_CLASSIFIER_MODEL/IMAGE_CLASSIFIER_MODEL/SPATIAL_*`），模型名不做白名单限制 |

### 4. 启动工作流
1. 点击仓库上方的 **Actions** 标签页。
2. 如果看到绿色按钮 **I understand my workflows...**，请点击启用。
3. 选择左侧的 `Epic Free Games` 工作流。
4. 点击右侧的 **Run workflow** 手动触发第一次运行测试。

> ✅ **成功提示**：之后的每周，脚本都会根据 `.github/workflows` 中的定时配置自动运行。


---


## 🐳 本地/Docker 部署

如果您拥有自己的服务器（VPS/NAS），可以使用 Docker Compose 一键部署。此版本已配置数据持久化，重启容器无需重新登录。

### 1. 获取代码
```bash
git clone https://github.com/10000ge10000/epic-awesome-gamer.git
cd epic-awesome-gamer/docker

```

### 2. 配置账号

直接编辑 `docker-compose.yaml` 文件，修改 `environment` 下的变量：

```yaml
version: '3'
services:
  epic-awesome-gamer:
    image: ghcr.io/10000ge10000/epic-awesome-gamer:latest
    environment:
      - EPIC_EMAIL=your_email@example.com      # <--- 修改这里
      - EPIC_PASSWORD=your_password            # <--- 修改这里
      - GEMINI_API_KEY=sk-xxxxxxxxxxxx         # <--- 修改这里
      # 可选：LLM 协议与地址（严禁代码私自改写 base_url）
      - LLM_MODE=gemini_native
      - LLM_BASE_URL=https://generativelanguage.googleapis.com
    # ...

```

### 3. 启动容器

```bash
docker compose up -d

```

> 💾 **关于数据持久化**：
> 容器启动后，您的登录凭证（Cookies）、截图和日志会自动保存在当前目录下的 `./volumes` 文件夹中。
> 即使删除或重启容器，只要 `./volumes` 文件夹还在，就不需要重新登录。

```

```

## 🛠️ 常见问题 (FAQ)

<details>
<summary><b>Q: 为什么日志显示 "Login with Email ... Timeout"?</b></summary>

**A:** 这是因为 GitHub Actions 的共享 IP 段可能被 Epic 临时风控。

* **现象**：脚本能打开页面，但在点击登录按钮时无反应。
* **解决**：通常 GitHub 会自动重试。如果连续失败，请等待 1-2 小时后手动重新运行工作流，GitHub 分配新 IP 后即可恢复。

</details>

<details>
<summary><b>Q: 使用中转 API 报错 "400 Bad Request" 或 "File API not supported"?</b></summary>

**A:** 请确保你使用的是本仓库的最新代码。

* 本项目使用自定义 `app/llm` 调用层，图片通过 **Inline Base64** 发送，不依赖 Gemini 的文件上传接口。
* 若被 WAF/Cloudflare 返回 HTML，日志会打印 `status_code` / `content-type` / `body_snippet`，方便快速定位是 401/403/5xx 还是网关拦截。

</details>

<details>
<summary><b>Q: 必须关闭二步验证 (2FA) 吗？</b></summary>

**A:** **是的，必须关闭。**
由于脚本运行在无头模式 (Headless) 下，无法处理短信或邮件验证码。请在 Epic 官网账户设置中暂时禁用 2FA。

</details>

---

## ⚠️ 免责声明

* 本项目仅供 Python 学习与技术交流使用。
* 使用脚本自动化操作可能违反 Epic Games 的服务条款，使用者需自行承担风险。
* 请勿将本项目用于任何商业用途。

---

<div align="center">
<b>Enjoy your free games! 🎮</b>
</div>

