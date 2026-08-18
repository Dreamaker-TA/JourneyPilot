"""不变量文档不许烂掉。

`docs/invariants.md` 的价值全部来自「它说的那个测试真的存在并且真的在跑」。一条引用了
被删掉的测试名的不变量比没有这条不变量更贵：它读起来像有人在守着，而没有人在守着。

所以这一份把文档当**代码**测：每个 `Tests:` 下面点名的 pytest 测试必须能被收集到，
每个 `Owner:` 里点名的文件必须存在，每个 ADR 链接必须指向一份真的文档。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_INVARIANTS = _REPO_ROOT / "docs" / "invariants.md"
_ADR_DIR = _REPO_ROOT / "docs" / "adr"


def _document() -> str:
    assert _INVARIANTS.exists(), "docs/invariants.md 缺失"
    return _INVARIANTS.read_text(encoding="utf-8")


def _invariant_blocks() -> list[tuple[str, str]]:
    """[(编号, 这一条的正文)]。"""

    text = _document()
    blocks: list[tuple[str, str]] = []
    parts = re.split(r"^### (INV-[A-Z]+-\d+)", text, flags=re.M)
    for index in range(1, len(parts), 2):
        blocks.append((parts[index], parts[index + 1]))
    return blocks


def _test_entries() -> list[tuple[str, str]]:
    """``Tests:`` 之下的每一条 ``- ...``，原样。

    **不能在第一个不匹配的行上停** —— ``Tests:`` 自己那一行的剩余部分是空串，一停就是
    零条引用，而一个收集到零条引用的门禁会永远通过。
    `test_the_gate_itself_collects_references` 钉住这一点。
    """

    entries: list[tuple[str, str]] = []
    for name, body in _invariant_blocks():
        section = body.split("Tests:", 1)
        if len(section) < 2:
            continue
        for line in section[1].splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                entries.append((name, stripped[2:].strip()))
    return entries


def _referenced_tests() -> list[tuple[str, str]]:
    """pytest 形状的引用：[(不变量编号, `文件::测试名` 或 `文件::*`)]。"""

    referenced: list[tuple[str, str]] = []
    for name, entry in _test_entries():
        match = re.match(r"`([^`]+)`", entry)
        if match and "::" in match.group(1) and not match.group(1).endswith(".ts"):
            referenced.append((name, match.group(1)))
    return referenced


def _referenced_frontend_tests() -> list[tuple[str, str]]:
    """前端 vitest 文件的引用（它们不经 pytest 收集，但同样不许指向不存在的文件）。"""

    referenced: list[tuple[str, str]] = []
    for name, entry in _test_entries():
        match = re.match(r"`([^`]+\.test\.tsx?)`", entry)
        if match:
            referenced.append((name, match.group(1)))
    return referenced


@pytest.fixture(scope="module")
def collected() -> tuple[set[str], set[str]]:
    """(全部 `文件::测试名`, 全部含测试的文件名)，从真实收集结果来。

    用 pytest 自己的收集而不是正则扫源码：参数化用例、跳过标记、conftest 影响的
    收集行为，只有收集器知道。
    """

    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--collect-only", "--no-header"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    node_ids: set[str] = set()
    files: set[str] = set()
    for line in result.stdout.splitlines():
        if "::" not in line or not line.startswith("tests/"):
            continue
        path, _, rest = line.partition("::")
        filename = Path(path).name
        files.add(filename)
        # 参数化用例带 `[...]`，比对时按裸测试名。
        test_name = rest.split("[", 1)[0].strip()
        node_ids.add(f"{filename}::{test_name}")
        # 允许文档用 `db/test_x.py::y` 这种带目录的写法。
        relative = str(Path(path).relative_to("tests"))
        files.add(relative)
        node_ids.add(f"{relative}::{test_name}")
    assert node_ids, f"收集不到任何测试：\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
    return node_ids, files


def test_the_gate_itself_collects_references():
    """守住这份门禁自己。

    解析器如果什么都没抓到，下面那条「引用的测试都存在」会**永远通过** —— 一个恒绿的
    门禁比没有门禁更贵。所以这里断言解析结果与文档规模相称：每条不变量至少一条引用。
    """

    blocks = _invariant_blocks()
    entries = _test_entries()
    assert len(blocks) >= 20, f"只解析出 {len(blocks)} 条不变量，解析器多半坏了"
    assert len(entries) >= len(blocks), (
        f"{len(blocks)} 条不变量只解析出 {len(entries)} 条测试条目"
    )
    covered = {name for name, _ in entries}
    assert covered == {name for name, _ in blocks}, (
        f"这些不变量没有解析出任何测试条目：{sorted({n for n, _ in blocks} - covered)}"
    )
    # pytest 形状的那一批要占大多数：如果解析器只认出前端条目，说明它坏了。
    assert len(_referenced_tests()) >= 30


def test_every_invariant_names_an_owner_and_a_test():
    """没有 owner 或没有测试的不变量只是一句愿望，而愿望不会响。"""

    incomplete = [
        name
        for name, body in _invariant_blocks()
        if "Owner:" not in body or "Tests:" not in body
    ]
    assert incomplete == [], f"这些不变量缺 Owner 或 Tests：{incomplete}"


def test_every_referenced_test_exists(collected):
    node_ids, files = collected
    missing: list[str] = []
    for invariant, reference in _referenced_tests():
        path, _, test_name = reference.partition("::")
        filename = Path(path).name
        if test_name == "*":
            if filename not in files and path not in files:
                missing.append(f"{invariant} → {reference}（文件不存在）")
            continue
        if f"{filename}::{test_name}" not in node_ids and reference not in node_ids:
            missing.append(f"{invariant} → {reference}")
    assert missing == [], (
        "不变量点名了不存在的测试（引用一个被删掉的测试比没有这条不变量更贵）：\n"
        + "\n".join(missing)
    )


_SEARCH_ROOTS = (
    Path("."),
    Path("src/travel_agent"),
    Path("frontend"),
    Path("migrations/versions"),
)


def _owner_reference_resolves(reference: str) -> bool:
    """Owner 写的是「谁保证这条」，形式有四种，全都要能落到磁盘上。

    - 明确的文件：``api/routes/chat.py``、``frontend/src/lib/x.ts``
    - 文件里的某个符号：``config/models.StrictConfig`` → ``config/models.py``
    - 一组文件：``configs/providers/*.yaml``
    - 一条迁移：``0003_local_identity``
    """

    candidates: list[str] = [reference]
    # 符号形式：取最后一个 `/` 之后第一个 `.` 之前那一段当模块名。
    tail_start = reference.rfind("/") + 1
    dot = reference.find(".", tail_start)
    if dot != -1 and not reference.endswith((".py", ".ts", ".yaml", ".json", ".md")):
        candidates.append(reference[:dot] + ".py")
    if not reference.endswith((".py", ".ts", ".yaml", ".json", ".md")):
        candidates.append(reference + ".py")

    for candidate in candidates:
        for root in _SEARCH_ROOTS:
            base = _REPO_ROOT / root
            if "*" in candidate:
                if any(base.glob(candidate)):
                    return True
                continue
            if (base / candidate).exists():
                return True
            # 迁移文件名带描述后缀（0001_baseline_current_schema.py）。
            if root.name == "versions" and any(base.glob(f"{candidate.removesuffix('.py')}*.py")):
                return True
    return False


_SOURCE_SUFFIXES = (".py", ".ts", ".tsx", ".yaml", ".json", ".sql", ".sh")


def _looks_like_a_path(reference: str) -> bool:
    """这个 backtick 里的东西是不是一个路径。

    Owner 行里也会出现符号名与命令（``commit_compaction``、``journeypilot config docs``），
    它们无法在文件系统里验证 —— 但**每条不变量至少要有一个可验证的路径**，那个断言
    在下面。这里只负责挑出「声称是路径」的那些。
    """

    if " " in reference or "=" in reference:
        return False
    return "/" in reference or reference.endswith(_SOURCE_SUFFIXES)


def _owner_paths(body: str) -> list[str]:
    owner_line = next((line for line in body.splitlines() if line.startswith("Owner:")), "")
    return [
        reference
        for reference in re.findall(r"`([^`]+)`", owner_line)
        if _looks_like_a_path(reference)
    ]


def test_every_referenced_frontend_test_exists():
    """前端测试文件不经 pytest 收集，但同样不许被引用到不存在的路径。"""

    missing = [
        f"{name} → {reference}"
        for name, reference in _referenced_frontend_tests()
        if not (_REPO_ROOT / reference).exists()
    ]
    assert missing == [], f"不变量点名了不存在的前端测试：{missing}"


def test_every_referenced_owner_file_exists():
    """Owner 里点名的路径必须真的存在 —— 否则「谁在保证」就没有答案。"""

    missing: list[str] = []
    for name, body in _invariant_blocks():
        for reference in _owner_paths(body):
            if not _owner_reference_resolves(reference):
                missing.append(f"{name} → {reference}")
    assert missing == [], f"Owner 指向不存在的文件：{missing}"


def test_every_invariant_owner_names_at_least_one_file():
    """只写一个符号名的 Owner 无法被验证，也无法被找到。"""

    vague = [name for name, body in _invariant_blocks() if not _owner_paths(body)]
    assert vague == [], f"这些不变量的 Owner 没有给出可定位的路径：{vague}"


def test_every_adr_link_resolves():
    text = _document()
    missing = [
        target
        for target in re.findall(r"\]\((adr/ADR-[^)]+\.md)\)", text)
        if not (_REPO_ROOT / "docs" / target).exists()
    ]
    assert missing == [], f"不变量链接到不存在的 ADR：{missing}"


def test_the_adr_index_lists_every_adr():
    """索引漏掉一份 ADR，那份 ADR 就等于不存在。"""

    index = (_ADR_DIR / "README.md").read_text(encoding="utf-8")
    on_disk = {path.name for path in _ADR_DIR.glob("ADR-*.md")}
    listed = set(re.findall(r"\((ADR-[^)]+\.md)\)", index))
    assert on_disk == listed, (
        f"索引与目录不一致：只在目录里 {sorted(on_disk - listed)}，"
        f"只在索引里 {sorted(listed - on_disk)}"
    )


def test_every_adr_states_its_alternatives():
    """一份不说「替代方案与为什么没选」的 ADR，读的人无法判断它是否还成立。"""

    incomplete = [
        path.name
        for path in sorted(_ADR_DIR.glob("ADR-*.md"))
        if "替代方案" not in path.read_text(encoding="utf-8")
    ]
    assert incomplete == [], f"这些 ADR 没写替代方案：{incomplete}"


def test_ci_and_compose_pin_the_same_database_image():
    """CI 与用户跑的必须是同一个数据库。

    `services:` 块里用不了 env 上下文，所以那个 digest 在四个文件里各写一份，改一处
    漏三处不会有任何红灯 —— 直到某次 nightly 对着一个旧镜像报绿。
    """

    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    pattern = re.compile(r"pgvector/pgvector:pg18@sha256:[0-9a-f]{64}")

    compose = pattern.findall((root / "docker-compose.yml").read_text(encoding="utf-8"))
    assert len(compose) == 1, f"compose 里应当只有一处 digest，实际 {len(compose)}"

    for name in ("pr", "nightly", "release"):
        text = (root / ".github" / "workflows" / f"{name}.yml").read_text(encoding="utf-8")
        for found in pattern.findall(text):
            assert found == compose[0], (
                f"{name}.yml 的 pgvector digest 与 docker-compose.yml 不一致：\n"
                f"  {found}\n  {compose[0]}"
            )
