# introspect

照见你与 AI 对话的语言与思考习惯 —— 一个跨 agent 的自我复盘管线。

introspect 从你本机的多个 coding agent（Codex / Claude Code / DSH / OMP）读取对话历史，抽取**你本人**的发言（agent 回复不计入统计），然后：

- **口癖统计**（确定性）—— 统计「一下 / 可能 / 就是 / 那个 / 然后…」等口头禅的累计出现次数。
- **语言习惯诊断**（LLM）—— 指出指代不清、指令模糊、冗余措辞等反复出现的问题，给出「原句 → 建议改写」。
- **每日思考复盘**（LLM）—— 复盘「哪些问题换个问法更好 / 在哪绕了圈 / 该问却没问 / 一直想错了」，并给出一句话教训。
- **自改进** —— 复盘模型会提出对复盘方法本身的改进建议，累积在 `state/reflect_improvements.md`。

## 支持的数据源

| Agent | 会话存储 |
|---|---|
| Codex | `~/.codex/sessions/**/*.jsonl` |
| Claude Code | `~/.claude/projects/**/*.jsonl` |
| DSH | `~/.dsh/sessions/**/session.v4.jsonl.zstd`（流式 zstd） |
| OMP | `~/.omp/agent/sessions/**/*.jsonl` |

## 依赖

- Python 3.11+
- `pip install zstandard`（仅 DSH 数据源需要）
- DeepSeek API key（语义分析用），从环境变量 `DEEPSEEK_API_KEY` 读取；未设置时回退到 `~/.dsh/.credentials.yaml`。

## 安装

```bash
cp config.example.json config.json   # 按需修改文档路径
pip install zstandard
```

## 使用

```bash
python habit_feedback.py run       # 口癖统计 + 语言习惯 + 全局记忆维护（每日入口）
python habit_feedback.py reflect   # 每日思考复盘
python habit_feedback.py stats     # 仅确定性口癖统计
python habit_feedback.py digest    # 生成新消息摘要（供 LLM）
```

`run` 等价于 `stats + digest + llm`。`llm` 用 DeepSeek 做语义维护，会更新桌面「语言习惯观察.md」与 `~/.codex/AGENTS.md`、`~/.claude/CLAUDE.md`。

## 定时

Windows 任务计划程序示例（每天 22:15 / 22:30）：

```powershell
schtasks /Create /TN habit-feedback-daily /TR "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\path\to\introspect\run.ps1" /SC DAILY /ST 22:15
schtasks /Create /TN habit-reflect-daily   /TR "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\path\to\introspect\reflect.ps1" /SC DAILY /ST 22:30
```

### Linux / macOS（cron）

一键安装（幂等，重复运行会替换旧条目）：

```bash
bash install-cron.sh
```

或手动 `crontab -e` 加入：

```cron
15 22 * * * /path/to/introspect/run.sh >> /path/to/introspect/state/cron.log 2>&1
30 22 * * * /path/to/introspect/reflect.sh >> /path/to/introspect/state/cron.log 2>&1
```

> cron 的 PATH 很精简；若 `python3` 不在 `/usr/bin`，请把 `run.sh` / `reflect.sh` 里的 `python3` 改成绝对路径（`command -v python3` 查看），或在 cron 行首加 `PATH=/usr/local/bin:/usr/bin:/bin`。

## 隐私

- 代码里不含任何密钥；DeepSeek key 在运行时从环境变量 / 凭据文件读取，不落盘到仓库。
- 运行产生的 `state/`（含你的真实对话摘要）与本地 `config.json` 已被 `.gitignore` 排除，请勿提交。
- 发布或提交前请自行检查 `state/` 与 `config.json` 的内容。

## 文件结构

```
habit_feedback.py     主脚本（4 个适配器 + stats / digest / llm / reflect）
reflect_prompt.md     复盘提示词（可编辑，复盘方法论演进不必改代码）
config.example.json   配置模板（复制为 config.json 后本地修改）
run.ps1 / reflect.ps1 Windows 定时包装脚本
run.sh / reflect.sh   Linux / macOS 定时包装脚本
install-cron.sh       安装 cron 任务（幂等）
state/                运行状态（水位线、计数、摘要、自改进建议），不入库
```
