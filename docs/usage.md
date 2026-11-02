# 第一版使用说明

以下为底层 CLI 调试说明。日常用户直接向 Codex 上传 EPUB，由 Agent 接管流程，见 [Codex Agent](agent.md)。

## 安装与启动

需要 Python 3.11+，运行时没有第三方 Python 依赖，也不需要 API key。

```sh
python -m pip install -e .
ebook-translate --version
```

不安装时，在仓库根目录用 `python -m ebook_translate` 替代 `ebook-translate`。命令输出统一 UTF-8。命令成功退出码为 0，输入或校验错误为 2。

Windows 若 pip 提示 Scripts 目录不在 PATH，可用仓库自带的 `.\ebook-translate.cmd`；不修改系统环境变量。

## 创建任务

```sh
ebook-translate init "D:/books/book.epub" --job "runs/book" --target zh-CN --max-chars 6000
ebook-translate status "runs/book"
ebook-translate next "runs/book"
```

任务目录必须尚不存在。程序读取 OPF 与 spine，提取所有 manifest XHTML 文档的正文，包括导航、附录和脚注，并提取 OPF 书名。每个文本单元有稳定 ID、原文哈希和回填位置。

`--max-chars` 为分批目标，计算正文及 JSON 字段开销；不拆开单个段落，超长单段会产生警告。不是模型 token 上限保证。

任务包含 `source.epub`、`job.json` 和 `responses/`。源 EPUB 被修改会拒绝恢复；任务 JSON 采用原子写入，写操作使用排他锁。崩溃残留 `.job.lock` 时，确认没有写进程后可移除该锁。任务目录内含原文和译文，适合放在被 Git 忽略的 `runs/` 中。

翻译开始前可配置术语和文风：

```sh
ebook-translate configure "runs/book" --glossary "glossary.json" --style "忠实原意，保留第一人称与冷幽默；简体中文自然流畅。"
```

术语表格式是 `{"原文人名":"中文译名"}`。已有批次开始后不允许改写全书规范，避免混用版本。

## 免费 Gemini 网页翻译

```sh
ebook-translate serve "runs/book" --port 8765
```

打开 `http://127.0.0.1:8765/`：选择批次 → 将提示词复制到 Gemini 网页 → 确认网页收到后记录已提交 → 等生成结束 → 将完整回复粘贴回来保存并校验 → 对照原文复核 → 填写说明并验收。

工作台不自动调用 Gemini、不接管浏览器、不保存账号凭据。安装随仓库分发的 skill 后，具有浏览器控制能力的 AI 助手可替用户执行上述网页操作。浏览器自动化依赖宿主，skill 本身不提供浏览器驱动。

任务持久化，不依赖服务进程或聊天记录。可关闭工作台后再次启动继续。服务默认只绑定 127.0.0.1，校验 Host、Origin 和表单 token；不要将其代理到公网。

### 不使用工作台时

```sh
ebook-translate prompt "runs/book" b0001 --output "runs/prompt.txt"
ebook-translate submitted "runs/book" b0001 --conversation "https://gemini.google.com/app/..."
ebook-translate import "runs/book" b0001 "runs/reply.json"
ebook-translate approve "runs/book" b0001 --note "对照原文复核了人物称谓、数字、否定与标签位置；说明具体警告处置。"
```

提示词要求 Gemini 返回：

```json
{
  "batch_id": "b0001",
  "items": [
    {"id": "使用提示词中的原ID", "source_hash": "使用提示词中的原哈希", "translation": "完整译文"}
  ]
}
```

`[[ET0001]]` 形式的标记保护原书内联标签；不得移动、增删或改写。人名、数字和上下文由模型理解，模型不能修改 HTML 标签。导入检查条目 ID 的集合与顺序、原文哈希、非空译文和保护标记；同时报告数字、原文未变与日文假名警告。

每次导入保存原始回复。错误回复将批次标记 `needs_revision`，不会被验收。重导入会撤销之前的验收；人工修改使用 `--provenance human-edit`。

上述是 CLI 整批调试流程。v0.3.0 的 Codex Agent 经 MCP 自动保存有效条目、缩小失败请求并局部补译，见 [恢复协议](agent.md#自动缩批与局部修复v030)。已经进入局部修复的批次请继续用 MCP，CLI 整批 import 会拒绝覆盖其恢复状态。

### 恢复状态

| 状态 | 下一步 |
| --- | --- |
| pending | 生成提示词并提交网页 |
| submitted | 先核对保存的对话，收取已有回复，避免重复发送 |
| needs_revision | 修正错误、要求 Gemini 重发完整批次，再导入 |
| review | 对照原文复核警告和文学质量，再 approve |
| accepted | 该批次可回填，继续下一批 |

`next` 优先返回已提交待收取、需修订、待复核的任务，然后才是待提交任务。

## 导出

```sh
ebook-translate build "runs/book" --output "outputs/book.zh.epub"
```

必须全部文本批次验收，导出文件位于任务目录之外。拒绝覆盖已有 EPUB 或报告。回填原文档树的文本，保留段落节点、样式引用、内联元素、链接和锚点；原 CSS、图片与 spine 顺序不变。书名和语言元数据随目标语言更新。

纯图片页默认阻止导出。确认是保留的封面或装饰页后，可显式添加 `--allow-image-pages`；报告仍将图片文本覆盖标为未验证。该选项不能完成扫描正文的 OCR。

同时生成 `book.zh.report.json`，实际计算处理数量与覆盖警告，记录来源和验收说明，并检查 XML、资源/锚点引用及 ZIP CRC。mimetype 为 ZIP 首项且不压缩。

需要标准合规验证时另行执行 `epubcheck outputs/book.zh.epub`。第一版不内置 EPUBCheck，不会把它报告为已执行；阅读器视觉检查和文学质量也不自动认证。

## 第一版边界

- 支持 EPUB 3、可重排 XHTML；拒绝 EPUB 2、固定版式和包含 encryption.xml 的书籍（包括字体混淆）。
- 底层 CLI 不自行调用 OCR。v0.2.0 的 Codex Agent 创建图片任务，交给 Gemini 网页识别和翻译，再由 Codex 对照复核。没有图片任务时仍保留警告。
- code/pre/math/SVG/注音 rt/rp 保留原内容，含此类内容会产生警告。ruby 原注音可能不再适合中文，需校订。
- 保留属性值，暂不翻译 alt/title；非空此类文本会产生警告。XHTML head 标题与 EPUB 3 附带的旧式 NCX 不在翻译范围。
- XML 序列化可能规范化命名空间前缀、引号及空标签写法；这是保留文档结构，不是字节相同或像素级复刻。
- 数字警告允许人工确认合法的数字写法变化；结构通过不能证明没有语义漏译。
- 本地工作台、CLI 与 MCP 使用同一任务内核；不会调用付费服务。MCP 已实现，浏览器能力由 Codex 宿主提供。

## Skill 分发

将仓库的 `skills/ebook-translate` 文件夹复制到宿主支持的 skills 目录，并让宿主能执行已安装的 `ebook-translate`。无需将运行数据或浏览器登录信息放入 skill。

## 开发验证

```sh
python -m unittest discover -s tests -v
```

集成测试使用代码生成的原创微型 EPUB 和确定性的测试译文，不发送真实书籍，不依赖外部模型。这验证本地工程链路，不能代替真实 Gemini 网页兼容性或文学翻译质量测试。
