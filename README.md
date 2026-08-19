# 初中英语备课助手

这是一个面向初中英语教师的 Codex Skill。它可以根据人教版七、八年级英语教材，帮助你查询单元知识、整理语法重点、生成原创练习题，并制作学生版、教师版和答案卡。

它适合用来辅助备课、课后练习和阶段性测验。生成的内容需要教师根据班级情况审核后使用。

## 能做什么

- 查询指定年级、册次、单元的语法、词汇和知识点。
- 按指定单元和题型生成原创练习或小测。
- 输出学生版试卷、教师版试卷和答案卡。
- 在生成前检查题目范围、分值和教材单元是否一致。

## 三步开始使用

### 1. 安装

需要安装 Codex Desktop 或 Codex CLI，并准备 Python 3.9 或更高版本。

#### 方式一：手动安装

在终端进入本仓库目录后执行：

```bash
mkdir -p ~/.agents/skills
cp -R . ~/.agents/skills/middle-school-english
```

安装后重新打开 Codex。

#### 方式二：把仓库链接发给 Codex

也可以在有终端和网络权限的 Codex 会话中直接发送：

```text
请帮我安装并启用这个 Codex Skill：
https://github.com/deSkyl1ne/middle-school-english-skill

请将仓库克隆到 ~/.agents/skills/middle-school-english，读取并检查 SKILL.md。
如果目标目录已经存在，请先告诉我，不要直接覆盖。完成后告诉我如何在新会话中使用它。
```

Codex 完成安装后，重新打开会话再启用 Skill。

### 2. 启用

在新的 Codex 会话中使用 `$middle-school-english`，然后直接描述你的需求。

### 3. 直接提问

可以复制下面的例子开始：

```text
请使用 $middle-school-english，整理人教版七年级下册 Unit 1 的语法重点，按“知识点、例句、常见错误”说明。
```

```text
请使用 $middle-school-english，根据七年级下册 Unit 1 和 Unit 2，生成一份 20 分钟的小测。
要求：题目原创，包含选择题、填空题和一个简短写作题，并附教师版答案和评分要点。
```

```text
请使用 $middle-school-english，为八年级上册 Unit 3 生成一份学生版练习和一份教师版答案，最后输出可打印的 PDF。
```

如果需求中的年级、单元、题型或分值不明确，Codex 会先询问必要信息。

## 当前支持范围

目前发布了人教版以下四册的逻辑内容数据：

- 七年级上册
- 七年级下册
- 八年级上册
- 八年级下册

目前没有发布九年级内容。这里的数据用于知识查询和原创练习生成，不声称精确对应某一次实体印刷版本，也不包含教材 PDF 或其他第三方源文件。

## 使用提醒

- 这是备课辅助工具，不替代教师对题目、答案和难度的审核。
- 生成的是原创练习，不应要求它大段复制教材原文或现成试题。
- 听力任务可以生成题目和脚本，但不会自动生成音频文件。
- PDF 打印流程会自动准备隔离的打印环境，不需要手动把打印依赖安装到全局 Python。

## 项目文件

- `references/`：教材目录、来源信息和结构化知识数据。
- `schema/`：请求、题目和输出格式定义。
- `scripts/`：查询、组卷、打印和验证脚本。
- `tests/`：打印、Schema 和内容一致性测试。

## 开发者验证

下面的命令用于维护仓库，不是教师日常使用的必要步骤：

```bash
python3 scripts/runtime_doctor.py --core
python3 scripts/validate_release.py --require-released
python3 scripts/query_knowledge.py \
  --book grade-07-semester-2 \
  --unit unit-01 \
  --domain grammar
```

预期结果分别包含 `CORE_RUNTIME_OK`、`RELEASE_VALIDATION_PASS` 和 `status: OK`。

如需验证打印流程：

```bash
python3 scripts/run_print.py \
  --request tests/fixtures/print-positive/render-request.json \
  --bundle-out tmp/print-bundle \
  --runtime-root .runtime/print
```

实际打印请统一使用 `scripts/run_print.py`，不要直接调用底层渲染或预检脚本。

