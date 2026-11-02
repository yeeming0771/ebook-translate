# Codex Agent 接入与使用

## 日常入口

在已接入工具的 Codex 会话上传 EPUB，说“用 Gemini 网页翻译成中文，保留 EPUB 格式”，等待译本与报告。

不需要操作命令行、工作台、搬运回复或逐批验收。Codex 自己进行对照复核。只有登录、验证码、额度限制、宿主权限或会话运行限制等外部阻碍可能需要交接，工具保存进度，不会假装完成。

## 一次性安装

```sh
python scripts/install_codex.py
```

使用当前 Python 绝对路径和 `scripts/run_mcp.py` 注册 stdio MCP，通过 Codex CLI 只添加名为 `ebook-translate` 的服务，并在 `$CODEX_HOME/skills/ebook-translate` 安装 skill。默认 CODEX_HOME 为用户 `.codex`；可用 `--codex-home` 选择隔离配置目录。

重新打开 Codex 会话后加载。宿主必须具备浏览器控制能力；MCP 不自带浏览器或 Gemini 登录，不使用 API key，不改变宿主审批规则。

重复安装可以更新本工具管理且未被修改的 skill；遇到其他同名配置或用户修改则拒绝覆盖。安装后仓库需保留在原位置。

## Agent 接口

| 工具 | 职责 |
| --- | --- |
| ebook_translate | 根据附件真实路径、会话工作区和目标语言创建或恢复任务 |
| ebook_next | 获取下一步动作、完整提示词、图片资源或对照复核材料 |
| ebook_configure | 首批前设定全文文风和人物译名术语表 |
| ebook_submitted | 网页实际收到请求后记录批次和对话地址 |
| ebook_receive | 保存实际网页回复并校验，失败转修订 |
| ebook_review | Codex 对照复核后验收并记录具体说明 |
| ebook_repair | 对待复核批次指定错误 ID 和原因，只重译这些条目 |
| ebook_finish | 全部完成后导出，返回 EPUB 和报告路径 |
| ebook_pause / ebook_resume | 记录外部阻碍并恢复原任务 |

用户不直接操作这些接口。MCP 执行本地文档工程；Codex 通过宿主浏览器操作 Gemini。服务本身不会独立接管浏览器。

## 状态与文件

任务位于会话工作区 `work/ebook-translate/<原书哈希>-<目标语言>/`。相同附件、工作区和语言重复进入会恢复已有任务，不依赖模型聊天记忆。导出位于会话 `outputs/`，拒绝覆盖未知产物；重复 finish 验证任务版本和产物哈希后返回已导出的文件。

图片任务：提取原图 → Gemini 分类、OCR、翻译 → Codex 对照原图复核 → 正文扫描页回填文字，或保留封面插画并说明。文字页不能作为装饰页跳过。混合页面图片内文字、复杂 SVG、alt/title 和原 ruby 注音仍有覆盖限制，报告保留警告。

文本任务按 pending、submitted、review、accepted 推进，坏回复转 needs_revision。所有动作由 Codex 完成；submitted 优先查看已有网页请求，避免重复发送。文本与图片任务均验收后才允许导出。

## 自动缩批与局部修复（v0.3.0）

文本回复校验失败后，独立校验每个完整条目的 ID、源文哈希和保护标记，保存有效项。乱序可按原 ID 恢复原顺序；重复 ID、错误哈希、损坏字段和标记均不猜测修正。JSON 截断时仅保留完整前缀对象，不跳过破损对象寻找后面的文本。此前的失败任务会自动重放已归档回复，兼容旧任务。

首次补译最多取原批次条目数的一半（向上取整），只发送仍缺失的条目。每次失败继续减半，至少一条；单条连续失败三次后返回 blocked 并保留进度。缩小的是请求范围，原批次、段落 ID 和回填位置保持稳定，不拆开单个长段落。此策略适用于文本；图片任务沿用原流程。

用户明确要求重试时，ebook_resume 可重开耗尽条目的重试预算，保留已有译文和历史，不自动无限重试。

局部语义错译由 Codex 调用 `ebook_repair(job, batch_id, unit_ids_json, reason)`；例如 `unit_ids_json='["d0001-u00002"]'`。每次最多三个指定条目起步，其余译文保留。完成后重组为原批次，严格整批校验，再由 Codex 对照复核；不会自动验收。

修复请求含持久化 request_id，重复获取下一步不会生成新编号；提交记录绑定编号与原网页对话。旧回复只归档，不覆盖当前候选。每个保留项记录对应原始回复和来源，导出报告包含逐项来源及修复历史。进入局部修复的任务使用 MCP 接口继续，底层 CLI 的整批 import 会拒绝覆盖，以防恢复状态失配。

## 验证范围

测试覆盖附件到 EPUB 的本地调度、MCP 握手和工具发现、修订、断点恢复、图片译文回填和重复导出。图片测试使用确定性回复，不代表实际 OCR 准确率。

真实 Gemini 网页长任务兼容性仍需在有浏览器控制能力的 Codex 会话中实测。安装本工具不能绕过登录、验证码、额度、宿主权限及会话运行限制；不会更换付费服务或启动后台模型。

## 官方接入参考

- [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Codex Skills](https://learn.chatgpt.com/docs/build-skills)
- [MCP stdio](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
