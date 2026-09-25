# 🚀 Feishu-SiYuan Copilot (飞书 × 思源笔记 24h 智能个人秘书)

<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.9+](https://img.shields.io/badge/Python-3.9%2B-brightgreen.svg)](https://www.python.org/)
[![Hardware: Apple Silicon MPS](https://img.shields.io/badge/Hardware-Apple%20Silicon%20MPS-orange.svg)]()
[![Feishu: WebSocket](https://img.shields.io/badge/Feishu-WebSocket%20v2-blue.svg)]()
[![Zero Public IP](https://img.shields.io/badge/Network-Zero%20Public%20IP-success.svg)]()

**专为知识工作者、终身学习者打造的 24/7 私人认知外脑与行动闭环系统。**  
采用「飞书手机端单向随时输入 + 本地思源笔记归集 + Apple Silicon (MPS) 毫秒级本地向量检索 + 混合大模型矩阵（带额度耗尽自动容灾降级）」。

[功能特性](#-核心特性) • [系统架构](#-系统架构) • [快速开始](#-快速开始) • [自然语言打标](#-零心智负担自然语言打标) • [常见问题](#-常见问题)

</div>

---

## 🌟 核心特性

- 🔒 **100% 绝对隐私（Zero Cloud Vector Leak）**：
  - 个人日记、随笔与本地书籍知识库 100% 保存在本地 SQLite 数据库中。
  - 深度利用 Apple Silicon (M系列芯片) 的 MPS 硬件加速，毫秒级就地进行 BAAI/bge-base-zh-v1.5 语义编码，**绝不将私人知识库上传至云端任何第三方向量库**。
- 🌐 **零公网 IP / 零端口映射（Zero Public IP Needed）**：
  - 基于飞书官方 WebSocket 长连接协议向外主动握手建联，只要 Mac 能上网，无论身处家庭 Wi-Fi、咖啡厅或手机热点，手机发出的飞书消息 0.1s 本地极速响应。
- 🛡️ **双引擎动静分离与零宕机自动保底（Zero Downtime）**：
  - **主思考引擎**：接入 DeepSeek-V3 / Qwen-72B 等高性能推理通道，负责认知洞见与日程解析；
  - **静态视觉 OCR 专线**：绑定智谱 GLM-4V-Flash 视觉大模型，实现书籍拍照、文献拍页自动识字提炼（永久 0 成本）；
  - **无缝自动容灾**：当主付费通道遇欠费 (402)、限流 (429) 或网络超时，系统 **0.1s 自动无缝降级** 至免费备选通道，并在消息尾部标注 `(保底)`，24 小时永不断联。
- 💡 **「Deja Vu」典籍与往日灵感回响**：
  - 每次随手记下一句随想，系统自动在本地 11,000+ 条书库与往日笔记中计算余弦相似度，提取契合度最高的往日印证与典籍片段，生成跨时空的「印证 · 归纳 · 待办 · 洞见」。
- 🏷️ **零心智摩擦自然语言打标（Natural Tagging）**：
  - 摆脱在手机小键盘切换 `#` 号的痛苦，支持句首/句尾自然语言识别（如“思考：”、“存档”、“【孩子教育】”）；
  - 智能分离「纯净资料存档（0 Token 消耗）」与「深度认知提炼」。
- ⏰ **双通道时间提醒与自然语言纠错**：
  - 本地 macOS 定时调度引擎与飞书官方待办中心双向同步；
  - 支持自然语言修改（“上面时间弄错了，改成明天下午3点”）。

---

## 🏗️ 系统架构

```text
  📱 手机 / 桌面飞书 (任何外网环境)
            │ (向外主动建立 WebSocket 长连接，无需公网IP)
            ▼
┌────────────────────────────────────────────────────────┐
│  💻 本地 Mac 终端 (Feishu-SiYuan Copilot 守护进程)        │
│                                                        │
│  [1] 自然语言解析器 ────► 纯净存档 / 深度思考 / 时间待办 │
│  [2] 本地向量底座 (Apple Silicon MPS + BGE 向量模型)     │
│       └─ 检索: 本地图书典籍 + 历史思源日记 (100% 隐私)  │
│  [3] 混合 LLM 矩阵网关                                 │
│       ├─ 主推理: SiliconFlow / DeepSeek-V3             │
│       ├─ 视觉OCR: 智谱 GLM-4V-Flash (书籍拍照提取)     │
│       └─ 自动容灾: 智谱 GLM-4-Flash (免费保底)         │
│  [4] 动作执行网关 (Action Gateway)                     │
│       ├─ 思源笔记内核 API (http://127.0.0.1:6806)      │
│       ├─ 飞书官方待办中心 API                          │
│       └─ 本地调度队列 (reminders.json)                 │
└────────────────────────────────────────────────────────┘
```

---

## 🚀 快速开始

### 1. 环境准备
- 操作系统：macOS (推荐搭载 Apple Silicon M系列芯片的 Mac)
- Python 版本：Python 3.9+
- 本地笔记软件：[思源笔记 (SiYuan Note)](https://b3log.org/siyuan/)

### 2. 克隆项目与安装依赖
```bash
git clone https://github.com/your-username/feishu-siyuan-copilot.git
cd feishu-siyuan-copilot

# 安装依赖
pip3 install -r requirements.txt
```

### 3. 配置凭证
复制模板文件并填入你的配置信息：
```bash
cp config.example.json config.json
```

需要准备的凭证：
1. **飞书开放平台** (https://open.feishu.cn/)：
   - 创建企业自建应用，获取 `App ID` 和 `App Secret`；
   - 权限申请：`im:message:send_as_bot` (以应用身份发消息), `im:message.p2p_msg:readonly` (读取单聊消息), `task:task:write` (任务管理);
   - 事件订阅：添加 **「接收消息 (im.message.receive_v1)」**，并将通讯模式选择为 **「长连接」**（无需填写公网 Webhook URL）。
2. **思源笔记**：
   - 设置 ➔ 关于 ➔ 开启 API 伺服，获取 `API Token` 与端口号 (默认 6806)。
3. **大模型 API Key**：
   - 推荐配置 SiliconFlow / DeepSeek 官方 API，并保留智谱 GLM-4-Flash 作为免费容灾保底。

### 4. 启动与守护进程安装
```bash
# 方式 A：前台运行调试
bash start.sh

# 方式 B：一键安装为 macOS 开机自启后台守护进程 (LaunchAgent)
bash install_autostart.sh
```

---

## 🏷️ 零心智负担自然语言打标

| 触发方式 | 示例 | 系统行为 |
| :--- | :--- | :--- |
| **纯净存档** | `存档 现代投资组合理论要点...` 或 `...资料保存` | **0 Token 消耗**，0.1s 极速写入思源日记并就地向量化，返回极简存档回执 |
| **深度思考** | `思考：今天关于复利的推导...` 或 `...随想` | 触发 **Deja Vu 典籍回响**，调用 DeepSeek-V3 输出「印证 · 归纳 · 待办 · 洞见」 |
| **项目标签** | `【孩子教育】下学期数学规划...` | 自动剥离括号标签，存入思源并附带结构化元数据 |
| **时间待办** | `明天下午3点提醒我给张总打电话` | 自动创建飞书待办卡片 + 本地精确秒级闹钟推送 |
| **模型切换** | 在飞书直接发送 `切换模型 硅基ds` | 在线热切换主推理大模型，无需重启服务 |

---

## 📄 开源许可证

本项目基于 [MIT 许可证](LICENSE) 开源。欢迎提交 Pull Request 与 Issue。
