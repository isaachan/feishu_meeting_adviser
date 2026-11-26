---
name: release-tag
description: Use when the user asks to 打 tag、打版本标签、定版、切版本、锁版本、标记可交付版本, or asks for a release tag. Creates a repo-level date-time annotated tag (release-YYYY.MM.DD-HHMM), writes release-notes/<tag>.md from commits, checks module versions, runs all local tests, commits, tags, and pushes. Does not deploy, does not wait for CI, and does not create module tags unless explicitly requested.
---

# 版本打标（release-tag）

## 目标与边界

为当前仓库生成一个可交付的**仓库级时间 tag**，写好版本说明，跑完本地测试，然后推送 commit 和 tag。

用“版本打标 / 打 tag / 定版”指代本流程，不要用“发布 / 上线”指代，避免和 server 上的生产部署混淆。

明确不做：

- 不 SSH、不执行 `scripts/prod-up.sh` / `prod-down.sh`、不做生产部署或生产验证。
- 不把 CI 绿作为打 tag 的门槛。CI 绿是 server 部署前的门槛。
- 默认不创建模块 tag（方案 A）。只有用户明确要求时才按文末附录处理。
- 不修改业务代码。除版本说明和用户确认的组件版本 bump 外，不提交其他文件。
- 不移动、不删除已经推送的 tag。

## 关键约定

| 项目 | 约定 |
|---|---|
| 仓库 tag | `release-YYYY.MM.DD-HHMM`，本地时间 `Asia/Shanghai`，月/日/时/分零填充 |
| 版本说明 | `release-notes/<tag>.md` |
| 组件版本来源 | `<module>/pyproject.toml` |
| 上一仓库 tag | `git describe --tags --abbrev=0 --match 'release-*'` |
| 推送 | commit 和 tag 都推到 origin |
| 首个 tag | 没有上一仓库 tag 时，版本说明做基线总结，不逐条罗列全部历史 |

## 执行流程

### 0. 预检

在仓库根目录执行。任何一项不满足就停止并说明原因，不要自动绕过。

```bash
ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"

git branch --show-current
git status --porcelain
git fetch origin
git rev-list --left-right --count origin/main...HEAD
LAST=$(git describe --tags --abbrev=0 --match 'release-*' 2>/dev/null || true)
printf 'LAST=%s\n' "$LAST"
```

规则：

- 当前分支必须是 `main`。不是则询问用户，不要自动切换。
- `git status --porcelain` 必须为空。有未提交内容则停止。
- `origin/main...HEAD` 的 behind 数必须为 0；ahead 可以大于 0，后面一起推送。
- `git fetch origin` 失败则停止。
- `LAST` 为空表示首个仓库 tag。

### 1. 生成并确认 tag 名

```bash
TAG="release-$(TZ=Asia/Shanghai date +%Y.%m.%d-%H%M)"
printf '%s\n' "$TAG"

git rev-parse -q --verify "refs/tags/$TAG" && echo 'local tag exists'
git ls-remote --tags origin "refs/tags/$TAG"
```

规则：

- 把建议 tag 名展示给用户，必须得到明确确认后才能继续。
- 本地或远端已存在同名 tag 时停止。不要自动加后缀，也不要复用旧 tag。
- 同一分钟重复执行会撞名；让用户等待下一分钟，或由用户明确决定改到秒。

### 2. 收集 commit 范围和组件版本

确定 commit 范围：

```bash
if [ -n "$LAST" ]; then
  RANGE="$LAST..HEAD"
else
  RANGE="HEAD"
fi
git log --no-merges --pretty=format:'%h%x09%s' "$RANGE"
```

- 有上一 tag：严格使用 `$LAST..HEAD`。
- 首个 tag：范围是仓库起点，但版本说明只做基线总结，不要把全部历史逐条贴进去。

列出本次改动的模块：

```bash
git diff --name-only "$RANGE" | awk -F/ 'NF>1 {print $1}' | sort -u
```

只保留顶层存在 `pyproject.toml` 的模块目录。`docs/`、`scripts/`、`integrations/` 等不算模块变更。

读取所有模块版本，形成版本矩阵：

```bash
for f in */pyproject.toml; do
  m=${f%/pyproject.toml}
  v=$(grep -m1 '^version[[:space:]]*=' "$f" | sed 's/.*=[[:space:]]*"//; s/".*//')
  printf '%s\t%s\n' "$m" "$v"
done
```

对本次改动过的模块，检查版本是否已 bump：

```bash
# 当前版本
grep -m1 '^version[[:space:]]*=' "$m/pyproject.toml"

# 上一仓库 tag 中的版本（首个 tag 跳过）
git show "$LAST:$m/pyproject.toml" | grep -m1 '^version[[:space:]]*='
```

规则：

- 模块有改动但版本号与上一 tag 相同：停止，列出未 bump 的模块，要求用户先 bump 并提交。不要自动改版本。
- 当前版本低于上一 tag：停止并询问。
- 上一 tag 不存在该模块：标记为新增模块。
- 未改动模块不需要 bump，版本矩阵里照常列出现有版本。

### 3. 写版本说明

```bash
mkdir -p release-notes
```

创建 `release-notes/<tag>.md`，使用以下结构：

```markdown
# <tag>

- 仓库 tag：`<tag>`
- 时间：<ISO 8601，带 +08:00>
- Commit：`<short sha>`
- 上一仓库 tag：`<last tag>` 或 `无（首个 tag）`

## 组件版本

| 模块 | 版本 | 本次是否变更 |
|---|---|---|
| advisor | 0.1.0 | 否 |
| ... | ... | ... |

## 变更

### 新功能

- ...

### 修复

- ...

### 重构 / 测试 / 文档

- ...

### 其他

- ...

## 升级与迁移

- 数据库 schema、契约、配置、依赖等需要人工处理的事项。
- 没有则写“无”。

## 部署与回滚

- 本 tag 只完成打标，不包含生产部署。
- 部署前需在 server 确认 CI 绿，再按 `docs/DEPLOYMENT.md` §7.2 手动执行。
- 回滚点：上一仓库 tag `<last tag>`；首个 tag 写“无上一 tag”。

## 本地验证

- 测试命令与结果。
- 明确写明：CI 未作为打 tag 门槛，部署前需确认 CI 绿。
```

内容要求：

- 从 commit 信息提取内容，按 `feat` / `fix` / `refactor` / `test` / `docs` / `chore` / `perf` 分组；非规范 commit 放到“其他”，改写成用户能看懂的描述。
- 突出破坏性变更、DB schema 迁移、契约变更、配置变更。
- 首个 tag 做能力基线和组件版本矩阵总结，不逐条罗列全部历史。
- 不要把未验证的事情写成已验证；CI 没跑就写“未作为门槛”，不要写“CI 通过”。

写完把摘要展示给用户确认，再进入提交。

### 4. 提交版本说明和版本 bump

```bash
git add "release-notes/$TAG.md"

# 只有用户确认过的组件版本 bump 才加入：
# git add <module>/pyproject.toml

git status --porcelain
git commit -m "chore(release): $TAG"
```

规则：

- 提交前确认 `git status --porcelain` 只包含本次版本说明和用户确认的版本 bump。
- 不要夹带无关改动。
- 不要自动 amend、rebase 或改写历史。

### 5. 跑本地全量测试

优先使用仓库已有的 `.venv-local`（由 `scripts/local-up.sh` 创建）。如果不存在，停止并让用户先准备环境，不要静默新建全局环境。

确保 `harness` 已安装（它不在 `local-up.sh` 的安装列表里）：

```bash
ROOT=$(git rev-parse --show-toplevel)
VENV="$ROOT/.venv-local"

if ! "$VENV/bin/pip" show llm-harness >/dev/null 2>&1; then
  "$VENV/bin/pip" install -e "$ROOT/harness[dev]"
fi
"$VENV/bin/python" -c 'import harness; assert harness.__file__' || {
  echo 'harness 仍是命名空间包，未安装到 .venv-local' >&2
  exit 1
}
```

注意：仓库根目录本身有一个 `harness/` 目录，未安装时 `import harness` 会命中命名空间包且 `__file__` 为 `None`。必须用 `pip show llm-harness` 或 `assert harness.__file__` 判断。

逐个模块跑静态检查和单测：

```bash
ROOT=$(git rev-parse --show-toplevel)
VENV="$ROOT/.venv-local"

for m in recorder advisor bot auth stt-server llm-gateway gui admin-site harness; do
  echo "== $m =="
  (
    cd "$ROOT/$m"
    "$VENV/bin/ruff" check src tests
    "$VENV/bin/pyright" --pythonpath "$VENV/bin/python"
    "$VENV/bin/python" -m unittest discover -s tests -v
  ) || exit 1
done
```

再跑跨模块集成测试：

```bash
ROOT=$(git rev-parse --show-toplevel)
VENV="$ROOT/.venv-local"

"$VENV/bin/ruff" check --config "$ROOT/advisor/pyproject.toml" "$ROOT/integrations"
(
  cd "$ROOT"
  "$VENV/bin/python" -m unittest discover -s integrations -v
)
```

规则：

- 任何一项失败都停止，不打 tag。修复后重新提交并重新跑全量测试。
- 测试必须在最终 commit 上运行，保证被打 tag 的树就是被测过的树。
- `.venv-local` 是 Python 3.14；若用户坚持在 3.11 上跑，`stt-server` 有已知 flaky 用例，必须如实报告，不能静默忽略。
- 记录测试命令和结果，写进交接摘要；版本说明里可先写命令，结果以实际为准。

### 6. 打 annotated tag

```bash
git status --porcelain
git rev-parse HEAD
git tag -a "$TAG" -m "$TAG"$'\n\n'"release-notes/$TAG.md"
git cat-file -t "$TAG"
```

规则：

- `git status --porcelain` 必须为空。
- `git cat-file -t` 必须返回 `tag`，不能是轻量 tag。
- 默认打在当前 HEAD。不要给旧 commit 打 tag，除非用户明确指定。
- 如果此前测试后又有任何提交，必须重新跑测试再打 tag。

### 7. 最终确认并 push

先展示交接摘要并得到用户明确确认，再推送：

```bash
git fetch origin
git rev-list --left-right --count origin/main...HEAD

git push origin HEAD:main
git push origin "$TAG"
```

推送后验证：

```bash
git ls-remote --tags origin "refs/tags/$TAG"
git status --porcelain
```

规则：

- 推送前如果 behind 数不为 0，停止，不要 force push。
- 不要 force push，不要移动已推送 tag。
- 如果 commit 已推送但 tag 推送失败：只重试 tag 推送，不要重建 commit 或换 tag 名。

### 8. 输出交接摘要

最后输出以下信息：

```text
仓库 tag：<tag>
commit：<short sha>
版本说明：release-notes/<tag>.md
组件版本：
  advisor <version>
  ...
本地测试：全部通过 / 失败（如实写）
CI：未作为打 tag 门槛；部署前需在 server 确认 CI 绿
生产部署：未执行
```

提示用户：部署仍按 `docs/DEPLOYMENT.md` §7.2 在 server 手动执行；部署前先确认 CI 绿。

## 失败与幂等

- tag 创建前任何一步失败：停止，修复后重新执行；不要打 tag。
- 本地或远端已有同名 tag：停止并询问；不要自动删除、移动或复用。
- 版本说明文件已存在：停止并询问，除非用户明确要求在打 tag 前更新。
- 测试失败后不要直接打 tag；修复、重新提交、重新跑全量测试。
- tag 一旦推送，视为不可变；需要修正就发新的时间 tag，不改旧 tag。

## 附录：模块 tag（默认不用）

方案 A 下，本 skill 不创建模块 tag。

只有用户明确要求时才执行：

- 命名：`<module>-vX.Y.Z`，例如 `advisor-v0.1.1`、`llm-gateway-v0.1.1`。
- 只给本次版本号发生变化的模块打，全部打在同一个 commit 上。
- 同一模块同一版本只能指向一个 commit；如果 tag 已存在且不在当前 HEAD，停止，说明版本没有 bump 或 tag 指错位置。
- 模块没变就不打新 tag，旧模块 tag 继续留在旧 commit。
- 模块 tag 不能表达“同一模块版本在每次仓库 tag 中的快照”；需要这种快照时，用版本说明里的版本矩阵，不要重复打同名 tag。
