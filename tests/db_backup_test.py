"""S6-FIX-02 数据库物理备份独立测试。

只用临时目录 / 临时 SQLite 库；**不碰生产 agent.db**，不依赖生产库数据，
不修改 dsh-memory 权威源，不新增依赖。

覆盖：
  1. 基本备份（含 DB-only memory、中文、特殊字符、外键关联）
  2. WAL 模式（源库保持打开时备份；备份可独立打开）
  3. 失败安全（源库不存在 / 不可读 / 目标不可写 / 备份失败 / 校验失败）
  4. 保留策略（最新 7 份、只删本工具产物、目录外不删、符号链接）
  5. 边界（命名正则、并发撞秒、dry-run 零副作用、幂等）

运行：python tests/db_backup_test.py
退出码：0 = 全部通过；1 = 有失败。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import _env_guard  # noqa: E402,F401  OPT-04a：数据库隔离闸门（必须在任何 app.* 之前）

from app import db_backup  # noqa: E402

PASS = 0
FAIL = 0
_TMPS: list[Path] = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def mktmp(prefix="s6fix02-"):
    p = Path(tempfile.mkdtemp(prefix=prefix))
    _TMPS.append(p)
    return p


# ---------------------------------------------------------------------------
# 造库
# ---------------------------------------------------------------------------
def make_db(path: Path, *, wal: bool = False, rows: int = 3) -> Path:
    """造一个带外键、含中文/特殊字符/DB-only 数据的临时库。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
        CREATE TABLE parents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        );
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            parent_id INTEGER REFERENCES parents(id)
        );
        """)
        conn.execute("INSERT INTO parents (id, name) VALUES (1, '父记录一')")
        conn.execute("INSERT INTO parents (id, name) VALUES (2, 'parent-two')")
        # DB-only memory：库里独有、无法从 dsh-memory 重建的两条
        conn.execute(
            "INSERT INTO memories (id, content, category, parent_id) VALUES (4, ?, 'preference', 1)",
            ("用户偏好喝冰美式咖啡，不加糖不加奶。",),
        )
        conn.execute(
            "INSERT INTO memories (id, content, category, parent_id) VALUES (5, ?, 'preference', 1)",
            ("用户要求文档用中文撰写，不要中英混杂。",),
        )
        # 普通记录 + 特殊字符
        special = "特殊字符：'单引号' \"双引号\" <标签> & % _ \\ / | 换行\r\n结束"
        conn.execute(
            "INSERT INTO memories (id, content, category, parent_id) VALUES (10, ?, 'fact', 2)",
            (special,),
        )
        for i in range(rows):
            conn.execute(
                "INSERT INTO memories (content, category, parent_id) VALUES (?, 'general', 2)",
                (f"普通记录 {i} —— 中文内容测试 {i * 7}",),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def snapshot_rows(db_path: Path) -> dict:
    """只读取出全部数据，用于比对原库与备份是否一致。"""
    conn = sqlite3.connect(db_backup._readonly_uri(db_path), uri=True)
    try:
        out = {}
        for table in ("parents", "memories"):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            out[table] = (tuple(cols), [tuple(r) for r in rows])
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. 基本备份
# ---------------------------------------------------------------------------
def test_basic():
    print("\n=== 1. 基本备份测试 ===")
    tmp = mktmp("s6fix02-basic-")
    src = make_db(tmp / "data" / "agent.db", wal=False)
    before = snapshot_rows(src)

    res = db_backup.create_physical_backup(db_path=src, keep=7)
    check("备份返回 ok=True", res.get("ok"), res.get("message"))
    if not res.get("ok"):
        return

    bak = Path(res["path"])
    check("备份文件真的存在", bak.is_file(), str(bak))
    check("备份文件大小 > 0", bak.stat().st_size > 0, str(bak.stat().st_size))
    check("文件名符合本工具规则", db_backup.is_backup_name(bak.name), bak.name)
    check("备份放在 backups/ 子目录",
          bak.parent.name == db_backup.BACKUP_SUBDIR, bak.parent.name)
    check("res.bytes 与实际一致", res["bytes"] == bak.stat().st_size)
    check("sha256 是 64 位十六进制",
          len(res["sha256"]) == 64 and all(c in "0123456789abcdef" for c in res["sha256"]))
    check("verified.integrity == ok", res["verified"]["integrity"] == "ok")
    check("verified 外键违规为 0", res["verified"]["foreign_key_violations"] == 0)

    # 备份独立可打开（用全新连接，不依赖源库）
    after = snapshot_rows(bak)
    check("原库与备份数据逐行一致", before == after)

    # DB-only memories 必须完整
    conn = sqlite3.connect(db_backup._readonly_uri(bak), uri=True)
    try:
        r4 = conn.execute("SELECT content FROM memories WHERE id=4").fetchone()
        r5 = conn.execute("SELECT content FROM memories WHERE id=5").fetchone()
        check("DB-only memory id=4 完整保留",
              r4 is not None and "冰美式咖啡" in r4[0], str(r4))
        check("DB-only memory id=5 完整保留",
              r5 is not None and "中文撰写" in r5[0], str(r5))
        # 中文与特殊字符逐字节一致
        orig = sqlite3.connect(db_backup._readonly_uri(src), uri=True)
        try:
            o4 = orig.execute("SELECT content FROM memories WHERE id=4").fetchone()[0]
            o10 = orig.execute("SELECT content FROM memories WHERE id=10").fetchone()[0]
        finally:
            orig.close()
        b10 = conn.execute("SELECT content FROM memories WHERE id=10").fetchone()[0]
        check("中文内容逐字符一致", r4[0] == o4, f"{r4[0]!r} != {o4!r}")
        check("特殊字符内容逐字符一致", b10 == o10, f"{b10!r} != {o10!r}")
    finally:
        conn.close()

    # 源库未被改动
    check("源库 sha256 与备份不同（是两个独立文件）",
          db_backup.sha256_file(src) != res["sha256"])
    check("源库仍可正常打开且数据未变", snapshot_rows(src) == before)

    # 没有残留临时文件
    leftovers = [p.name for p in (src.parent / db_backup.BACKUP_SUBDIR).iterdir()
                 if db_backup.PARTIAL_SUFFIX in p.name]
    check("没有残留 .partial 临时文件", leftovers == [], str(leftovers))

    # 备份目录里除备份本体外不该有任何 SQLite 伴生文件（端到端实测抓到过
    # -shm/-wal 孤儿堆积：verify_backup 以只读身份打开备份会建 -shm，而
    # os.replace 只搬主文件，那些残骸名字不匹配 FILE_RE 永远清不掉）。
    bdir = src.parent / db_backup.BACKUP_SUBDIR
    sidecars = [p.name for p in bdir.iterdir()
                if p.name.endswith(("-shm", "-wal", "-journal"))]
    check("备份目录没有 -shm/-wal/-journal 孤儿", sidecars == [], str(sidecars))
    check("备份是自包含单文件（不带 -wal）",
          not Path(str(bak) + "-wal").exists() and not Path(str(bak) + "-shm").exists(),
          "备份旁边出现了伴生文件")


# ---------------------------------------------------------------------------
# 2. WAL 模式
# ---------------------------------------------------------------------------
def test_wal():
    print("\n=== 2. WAL 模式测试 ===")
    tmp = mktmp("s6fix02-wal-")
    src = make_db(tmp / "data" / "agent.db", wal=True)

    # 确认真的是 WAL
    conn = sqlite3.connect(str(src))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    check("源库确实是 WAL 模式", str(mode).lower() == "wal", str(mode))

    # 关键：源库保持打开 + 有未 checkpoint 的写入时备份
    holder = sqlite3.connect(str(src))
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute(
            "INSERT INTO memories (content, category, parent_id) VALUES (?, 'fact', 1)",
            ("保持连接期间写入的数据",),
        )
        holder.commit()
        # 此刻数据在 -wal 里，agent.db 主文件可能还没带上
        wal_path = Path(str(src) + "-wal")
        check("存在 -wal 文件（有未 checkpoint 的数据）",
              wal_path.is_file() and wal_path.stat().st_size > 0,
              str(wal_path.stat().st_size) if wal_path.is_file() else "missing")

        res = db_backup.create_physical_backup(db_path=src, keep=7)
        check("WAL 模式备份成功", res.get("ok"), res.get("message"))
        if res.get("ok"):
            bak = Path(res["path"])
            check("备份校验通过", res["verified"]["integrity"] == "ok")
            # 备份必须包含刚写进 -wal 的那条
            bconn = sqlite3.connect(db_backup._readonly_uri(bak), uri=True)
            try:
                got = bconn.execute(
                    "SELECT count(*) FROM memories WHERE content LIKE '%保持连接期间%'"
                ).fetchone()[0]
                check("备份包含仅存在于 -wal 中的最新写入", got == 1, f"count={got}")
                # 备份自身不应带 -wal 附属文件（它已 checkpoint 成单文件）
                check("备份文件无需 -wal 即可打开",
                      not Path(str(bak) + "-wal").exists()
                      or Path(str(bak) + "-wal").stat().st_size == 0)
            finally:
                bconn.close()
    finally:
        holder.close()

    # 库关闭后备份仍可独立打开
    res2 = db_backup.create_physical_backup(db_path=src, keep=7)
    check("库关闭后再备份也成功", res2.get("ok"), res2.get("message"))
    if res2.get("ok"):
        v = db_backup.verify_backup(Path(res2["path"]))
        check("独立复核 integrity=ok", v["integrity"] == "ok")


# ---------------------------------------------------------------------------
# 3. 失败安全
# ---------------------------------------------------------------------------
def test_failures():
    print("\n=== 3. 失败安全测试 ===")
    tmp = mktmp("s6fix02-fail-")

    # 3.1 源库不存在
    missing = tmp / "nope" / "agent.db"
    res = db_backup.create_physical_backup(db_path=missing, keep=7)
    check("源库不存在 → ok=False", res.get("ok") is False)
    check("源库不存在 → 消息明确", "不存在" in res.get("message", ""), res.get("message"))
    check("源库不存在 → 未凭空创建源库", not missing.exists())
    check("源库不存在 → 未创建备份目录",
          not (tmp / "nope" / db_backup.BACKUP_SUBDIR).exists())

    # 3.2 源库是目录而不是文件
    weird = tmp / "adir"
    weird.mkdir()
    res = db_backup.create_physical_backup(db_path=weird, keep=7)
    check("源路径是目录 → ok=False", res.get("ok") is False)

    # 3.3 源库存在但不是 SQLite（随机字节）
    junk = tmp / "junk" / "agent.db"
    junk.parent.mkdir(parents=True, exist_ok=True)
    junk.write_bytes(b"this is definitely not a sqlite database" * 100)
    res = db_backup.create_physical_backup(db_path=junk, keep=7)
    check("源库不是 SQLite → ok=False", res.get("ok") is False, str(res.get("message")))
    check("源库不是 SQLite → 未留下正式备份",
          db_backup.list_backups(junk.parent / db_backup.BACKUP_SUBDIR) == [])
    check("源库不是 SQLite → 未留下临时文件",
          [p.name for p in (junk.parent / db_backup.BACKUP_SUBDIR).iterdir()
           if db_backup.PARTIAL_SUFFIX in p.name] == [])

    # 3.4 目标目录不可写：用一个"是文件不是目录"的路径占位
    src = make_db(tmp / "ok" / "agent.db")
    blocker = tmp / "blocked"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    res = db_backup.create_physical_backup(db_path=src, target_dir=blocker / "sub")
    check("目标目录不可创建 → ok=False", res.get("ok") is False, str(res.get("message")))
    check("目标失败 → 未破坏源库", src.is_file() and snapshot_rows(src)["memories"][1])

    # 3.5 校验失败：手工造一个"大小 >0 但不是合法 SQLite"的文件去 verify
    fake = tmp / "fake" / "agent_phys_20260101_000000.db"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_bytes(b"not a database at all" * 200)
    v = db_backup.verify_backup(fake)
    check("非法文件 → verify ok=False", v["ok"] is False)
    check("非法文件 → 报告无法打开", "无法作为 SQLite 库打开" in v["message"], v["message"])

    # 3.6 空文件
    empty = tmp / "empty" / "agent_phys_20260101_000001.db"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_bytes(b"")
    v = db_backup.verify_backup(empty)
    check("空文件 → verify ok=False", v["ok"] is False)
    check("空文件 → 报告大小为 0", "大小为 0" in v["message"], v["message"])

    # 3.7 verify 不存在的路径
    v = db_backup.verify_backup(tmp / "never" / "agent_phys_20260101_000002.db")
    check("不存在的备份 → verify ok=False", v["ok"] is False)
    check("不存在的备份 → 报告不存在", "不存在" in v["message"], v["message"])

    # 3.8 新备份校验失败时，历史有效备份必须不被清理
    sdir = tmp / "protect" / "agent.db"
    make_db(sdir)
    keep_dir = sdir.parent / db_backup.BACKUP_SUBDIR
    keep_dir.mkdir(parents=True, exist_ok=True)
    hist = []
    for i in range(3):
        p = keep_dir / f"agent_phys_2026010{i + 1}_030000.db"
        shutil.copy2(sdir, p)
        v = db_backup.verify_backup(p)
        check(f"历史备份 #{i + 1} 本身有效", v["ok"], v["message"])
        hist.append(p)

    # 让新备份必然校验失败：把目标目录里所有新建路径变成只读不可行（Windows 上
    # 不可靠），改成 monkeypatch verify_backup 强制返回失败——这模拟的正是
    # "备份逻辑跑完但自检没过"这条真实分支。
    orig_verify = db_backup.verify_backup
    try:
        db_backup.verify_backup = lambda p: {
            "ok": False, "path": str(p), "size": 10, "integrity": "malformed",
            "foreign_key_violations": None, "mode": "ro",
            "message": "integrity_check 未通过：模拟失败",
        }
        res = db_backup.create_physical_backup(db_path=sdir, keep=1)
    finally:
        db_backup.verify_backup = orig_verify

    check("校验失败 → 整体 ok=False", res.get("ok") is False, str(res.get("message")))
    check("校验失败 → 消息指出已丢弃", "已丢弃" in res.get("message", ""),
          res.get("message"))
    still = {p.name for p in db_backup.list_backups(keep_dir)}
    check("校验失败 → 三份历史备份一份不少", still.issuperset({p.name for p in hist}),
          str(still))
    check("校验失败 → 未留下临时文件",
          [p.name for p in keep_dir.iterdir() if db_backup.PARTIAL_SUFFIX in p.name] == [])

    # 3.9 keep 参数非法
    res = db_backup.create_physical_backup(db_path=src, keep=0)
    check("keep=0 → ok=False", res.get("ok") is False)
    res = db_backup.create_physical_backup(db_path=src, keep="abc")
    check("keep='abc' → ok=False", res.get("ok") is False)
    res = db_backup.create_physical_backup(db_path=src, keep=-3)
    check("keep=-3 → ok=False", res.get("ok") is False)


# ---------------------------------------------------------------------------
# 4. 保留策略
# ---------------------------------------------------------------------------
def test_prune():
    print("\n=== 4. 保留策略测试 ===")
    tmp = mktmp("s6fix02-prune-")
    src = make_db(tmp / "data" / "agent.db")
    base = src.parent / db_backup.BACKUP_SUBDIR
    base.mkdir(parents=True, exist_ok=True)

    # 造 10 份「本工具」备份，时间戳跨 10 天
    generated = []
    for i in range(10):
        p = base / f"agent_phys_202611{i + 1:02d}_050000.db"
        shutil.copy2(src, p)
        generated.append(p)

    # 干扰文件：非本工具命名、其他格式、子目录内的同名文件
    foreign_db = base / "agent.db.before-cleanup-20261030-220741"
    shutil.copy2(src, foreign_db)
    other_ext = base / "notes.txt"
    other_ext.write_text("不是备份", encoding="utf-8")
    # 名字像但不完全匹配（正则锚定必须挡住）
    lookalike1 = base / "agent_phys_2026_050000.db"          # 日期位数不对
    lookalike2 = base / "agent_phys_20261101_050000.db.bak"   # 后缀不对
    lookalike3 = base / "x_agent_phys_20261101_050000.db"     # 前缀不对
    for p in (lookalike1, lookalike2, lookalike3):
        shutil.copy2(src, p)

    # 目录外的同名文件（绝不能被删）
    outside = tmp / "agent_phys_20260101_050000.db"
    shutil.copy2(src, outside)

    pr = db_backup.prune_backups(keep=7, directory=base)
    check("prune 返回 ok=True", pr.get("ok"), str(pr))
    check("删除了 3 份", len(pr["removed"]) == 3, str(pr["removed"]))
    check("保留 7 份", pr["kept"] == 7, str(pr["kept"]))

    remaining = [p.name for p in db_backup.list_backups(base)]
    # list_backups 的契约是「新→旧」，所以期望值也要降序写。
    check("剩下的是最新的 7 份",
          remaining == [f"agent_phys_202611{i:02d}_050000.db" for i in range(10, 3, -1)],
          str(remaining))
    check("最旧的三份已被删除",
          not (base / "agent_phys_20261101_050000.db").exists()
          and not (base / "agent_phys_20261102_050000.db").exists()
          and not (base / "agent_phys_20261103_050000.db").exists())

    # 边界：非本工具文件一个都没动
    check("历史手工 .bak 文件未被删除", foreign_db.exists())
    check("其他格式文件未被删除", other_ext.exists())
    check("目录外同名文件未被删除", outside.exists())
    check("相似但不符合命名的文件未被删除",
          lookalike1.exists() and lookalike2.exists() and lookalike3.exists())

    # 4.2 dry-run 不删
    pr = db_backup.prune_backups(keep=2, dry_run=True, directory=base)
    check("dry-run 报告将删 5 份", len(pr["removed"]) == 5, str(len(pr["removed"])))
    check("dry-run 实际未删",
          len(db_backup.list_backups(base)) == 7, str(len(db_backup.list_backups(base))))

    # 4.3 protect 保护最新那份（list_backups 新→旧，[0] 才是最新）
    newest = db_backup.list_backups(base)[0]
    check("list_backups 第一个确实是最新的", newest.name.endswith("_20261110_050000.db"),
          newest.name)
    pr = db_backup.prune_backups(keep=1, protect=[newest], directory=base)
    check("protected 文件未被删除", newest.exists(), newest.name)
    check("protect 生效时保留数 >= 1", pr["kept"] >= 1, str(pr["kept"]))
    check("protect 的正是保留下来的那份",
          newest.name in pr["kept_names"], str(pr["kept_names"]))

    # 4.4 keep 非法
    for bad in (0, -1, "x"):
        try:
            db_backup.prune_backups(keep=bad, directory=base)
            check(f"prune keep={bad!r} 应抛 BackupError", False)
        except db_backup.BackupError:
            check(f"prune keep={bad!r} 抛 BackupError", True)

    # 4.5 备份目录不存在 → 干净返回
    pr = db_backup.prune_backups(keep=7, directory=tmp / "nope-dir")
    check("目录不存在 → ok=True 且不删任何东西",
          pr.get("ok") is True and pr["removed"] == [] and pr["kept"] == 0)

    # 4.6 符号链接：不跟随、不删
    link_target = tmp / "target-outside.db"
    shutil.copy2(src, link_target)
    link = base / "agent_phys_20250101_050000.db"
    try:
        os.symlink(link_target, link)
        linked = True
    except (OSError, NotImplementedError):
        linked = False
        print("  [INFO] 当前环境不允许创建符号链接，跳过该子项")
    if linked:
        pr = db_backup.prune_backups(keep=0 if False else 1, directory=base)
        check("符号链接不会被当成有效备份",
              link.name not in [p.name for p in db_backup.list_backups(base)])
        check("符号链接外的目标文件未被删除", link_target.exists())

    # 4.7 全链路：10 份 → 备份一次 → 只剩 keep 份
    tmp2 = mktmp("s6fix02-chain-")
    src2 = make_db(tmp2 / "data" / "agent.db")
    base2 = src2.parent / db_backup.BACKUP_SUBDIR
    base2.mkdir(parents=True, exist_ok=True)
    for i in range(10):
        shutil.copy2(src2, base2 / f"agent_phys_202611{i + 1:02d}_050000.db")
    res = db_backup.create_physical_backup(db_path=src2, keep=7)
    check("全链路备份成功", res.get("ok"), res.get("message"))
    if res.get("ok"):
        left = len(db_backup.list_backups(base2))
        check("全链路结束后正好 7 份", left == 7, str(left))
        check("新备份在保留名单里",
              Path(res["path"]).name in [p.name for p in db_backup.list_backups(base2)])
        check("pruned 报告了 4 份", len(res.get("pruned") or []) == 4,
              str(len(res.get("pruned") or [])))


# ---------------------------------------------------------------------------
# 5. 命名、并发、幂等、dry-run
# ---------------------------------------------------------------------------
def test_edges():
    print("\n=== 5. 命名/并发/幂等/dry-run ===")
    tmp = mktmp("s6fix02-edge-")

    # 5.1 命名正则
    valid = ["agent_phys_20261119_050000.db", "agent_phys_20261119_050000_001.db",
             "agent_phys_19990101_000000.db"]
    invalid = ["agent.db", "agent_phys_20261119.db", "agent_phys_20261119_050000.zip",
               "agent_phys_20261119_050000.db.partial-ab12cd34",
               "agent_phys_20261119_050000.db.bak", "我的备份.db", "",
               "AGENT_PHYS_20261119_050000.DB"]
    for n in valid:
        check(f"认可备份名 {n}", db_backup.is_backup_name(n))
    for n in invalid:
        check(f"拒绝非备份名 {n!r}", not db_backup.is_backup_name(n))

    # 5.2 同秒并发不覆盖
    src = make_db(tmp / "conc" / "agent.db")
    base = src.parent / db_backup.BACKUP_SUBDIR
    base.mkdir(parents=True, exist_ok=True)
    p1 = db_backup._choose_target(base, "20261119_050000")
    p1.write_bytes(b"x")
    p2 = db_backup._choose_target(base, "20261119_050000")
    check("同秒撞名会换一个文件名", p1 != p2, f"{p1.name} == {p2.name}")
    check("第二个名字带 _001 后缀", "_001" in p2.name, p2.name)

    # 5.3 同一秒连续两次备份都成功且互相不覆盖
    src2 = make_db(tmp / "conc2" / "agent.db")
    a = db_backup.create_physical_backup(db_path=src2, keep=10)
    b = db_backup.create_physical_backup(db_path=src2, keep=10)
    check("连续两次备份都成功", a.get("ok") and b.get("ok"))
    if a.get("ok") and b.get("ok"):
        check("两份备份是不同文件", a["path"] != b["path"],
              f"{a['path']} == {b['path']}")
        check("两份备份都在", Path(a["path"]).is_file() and Path(b["path"]).is_file())

    # 5.4 dry-run 零副作用
    src3 = make_db(tmp / "dry" / "agent.db")
    bdir = src3.parent / db_backup.BACKUP_SUBDIR
    before_files = sorted(p.name for p in bdir.iterdir()) if bdir.is_dir() else []
    before_size = src3.stat().st_size
    res = db_backup.create_physical_backup(db_path=src3, keep=7, dry_run=True)
    check("dry-run 返回 ok=True", res.get("ok") is True)
    check("dry-run 标记正确", res.get("dry_run") is True)
    check("dry-run 给出 would_write", bool(res.get("would_write")))
    after_files = sorted(p.name for p in bdir.iterdir()) if bdir.is_dir() else []
    check("dry-run 没有真的写文件", after_files == before_files,
          f"{before_files} -> {after_files}")
    check("dry-run 没有改动源库", src3.stat().st_size == before_size)

    # 5.5 幂等：重复跑结果一致（都是 ok，且文件数按 keep 收敛）
    src4 = make_db(tmp / "idem" / "agent.db")
    results = [db_backup.create_physical_backup(db_path=src4, keep=3) for _ in range(5)]
    check("连跑 5 次全部成功", all(r.get("ok") for r in results),
          str([r.get("message") for r in results if not r.get("ok")]))
    left = len(db_backup.list_backups(src4.parent / db_backup.BACKUP_SUBDIR))
    check("连跑 5 次后正好保留 3 份", left == 3, str(left))

    # 5.6 备份目录跟着库走（BIONIC_DB_PATH 语义的等价性）
    check("backup_dir() 默认落在库同级 backups/",
          db_backup.backup_dir().name == db_backup.BACKUP_SUBDIR)
    check("backup_dir() 的父目录是库所在目录",
          db_backup.backup_dir().parent == Path(db_backup.db.DB_PATH).resolve().parent)


# ---------------------------------------------------------------------------
def main() -> int:
    print("=== S6-FIX-02 数据库物理备份测试 ===")
    print(f"生产库路径（全程不使用）：{db_backup.db.DB_PATH}")
    prod = Path(db_backup.db.DB_PATH)
    prod_before = (prod.stat().st_size, prod.stat().st_mtime_ns) if prod.is_file() else None

    try:
        test_basic()
        test_wal()
        test_failures()
        test_prune()
        test_edges()
    finally:
        for p in _TMPS:
            shutil.rmtree(p, ignore_errors=True)

    # 全程不碰生产库的证明
    prod_after = (prod.stat().st_size, prod.stat().st_mtime_ns) if prod.is_file() else None
    check("生产库指纹全程未变（size + mtime_ns）", prod_before == prod_after,
          f"{prod_before} -> {prod_after}")

    print(f"\n=== 结果：{PASS} PASS / {FAIL} FAIL ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
