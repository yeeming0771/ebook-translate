# ebook-translate

以免费的 Gemini 网页版为翻译入口，保留 EPUB 结构的电子书翻译工作流。

## 设计前提

- 翻译工作由 Gemini 网页版完成，利用用户已有的浏览器登录会话。
- 不依赖付费模型 API，不在遇到限制或故障时自动切换模型。
- 本地工具负责 EPUB 解析、分批任务、进度保存、校验、回填与打包。
- Skill 负责浏览器操作、文学翻译要求、补译和异常恢复。
- 小说原文、译文、电子书文件、浏览器凭据和运行记录不进入 Git。

## Codex Agent v0.3.0

正常入口：在 Codex 会话上传 EPUB，说“翻译成中文”，等待 Codex 返回译本。

Codex 通过 MCP 自动建任务、操作免费 Gemini 网页、收取回复、对照复核、处理图片文字页并导出。用户不需要打开工作台、复制提示词或逐批确认。见 [Codex Agent 接入与使用](docs/agent.md)。

文本批次失败时保留有效条目，只补译错误项；再次失败自动减半请求，直到单条。新增 `ebook_repair` 可在复核发现局部错译时指定 ID 重译，合并后仍需整批复核。原始回复、逐项来源和修复历史可追溯。

一次性安装：

```sh
python scripts/install_codex.py
```

安装器只注册本项目 MCP 并安装可自动发现的 skill；无需 pip 或 PATH 配置。随后重新打开 Codex 会话加载。宿主需要浏览器控制能力及可使用的 Gemini 网页登录会话。

### 本地内核与开发入口

已提供可运行的 Python CLI、本地网页工作台和 AI 助手 skill。无需模型 API key，运行时无第三方 Python 依赖。

已实现：EPUB 3 全 manifest XHTML 清点、稳定 ID、内联标签保护、原文哈希校验、逐批状态和断点恢复、原始回复归档、对照验收、保结构回填、资源与锚点检查、ZIP 检查及报告。

## 可选的手动调试入口

需要 Python 3.11+：

```sh
python -m pip install -e .
ebook-translate init "D:/books/book.epub" --job "runs/book" --target zh-CN
ebook-translate serve "runs/book"
```

打开 `http://127.0.0.1:8765/`：选择批次 → 将提示词发给 Gemini 网页 → 记录提交 → 收取完整 JSON 回复 → 保存校验 → 对照原文复核并验收。

全部文本批次验收后：

```sh
ebook-translate build "runs/book" --output "outputs/book.zh.epub"
```

同时生成 `book.zh.report.json`，记录处理数量、来源、复核说明与未执行检查。

不安装时，可在仓库根目录用 `python -m ebook_translate` 替代 `ebook-translate`。原创微型书籍试用：

Windows 也可直接运行 `.\ebook-translate.cmd`，无需配置 Python Scripts 的 PATH。

```sh
python examples/create_demo.py --output "work/demo.epub"
python -m ebook_translate init "work/demo.epub" --job "runs/demo"
python -m ebook_translate serve "runs/demo"
```

## Gemini 网页 Skill

随仓库分发：[skills/ebook-translate/SKILL.md](skills/ebook-translate/SKILL.md)。将该文件夹复制到宿主 skills 目录并安装 CLI，即可让有浏览器控制能力的助手操作翻译循环。

CLI 不自行接管浏览器，skill 不自带浏览器驱动；Gemini 额度限制或登录中断时保存进度，不切换付费服务。

## 当前边界

支持无加密的可重排 EPUB 3，不支持 EPUB 2 和固定版式。Agent 会将纯图片页交给 Gemini 网页识别和翻译，由 Codex 对照原图复核。封面和插画可保留，扫描正文转换为可重排文字并记录版式变化。底层手动 CLI 没有图片任务时仍默认阻止纯图片页导出。

保结构不等于像素级复刻：XML 写法可能规范化，ruby 原注音、图形、代码和属性文本需复核。文学质量需对照原文判断，EPUBCheck 和视觉检查未执行时不会报告为通过。

完整命令与限制见 [使用说明](docs/usage.md)。

## 开发验证

```sh
python -m unittest discover -s tests -v
```

集成测试使用原创合成 EPUB 和确定性测试回复，覆盖 MCP 协议、Agent 自动调度、恢复及导出；不调用外部模型，不代表已完成真实 Gemini 长任务兼容性测试。

## 预期工作流

EPUB 检查 → 内容清单 → 提取与编号 → 网页分批翻译 → 对齐与校订 → 原结构回填 → 验证与导出。

详见 [工作流与实现计划](docs/workflow.md)。

## 版本同步

```sh
git status
git add <需要同步的项目文件>
git commit -m "Describe the change"
git push
```

拉取远程更新：

```sh
git pull --ff-only
```

推送只同步已提交的项目文件；被忽略的书籍、译文和本地运行数据不会同步。
