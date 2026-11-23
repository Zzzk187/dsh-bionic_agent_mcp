"""OPT-01 SQLite 只读 URI 编码安全测试。

背景：`app/portable_backup.py` 与 `app/portable_restore.py` 曾有 8 处用手写
f-string 拼只读 URI（`f"file:{path}?mode=ro"`）。路径里出现 `#` 会被当成 URI
片段分隔符、`%` 会被当成转义序列前缀，导致：
  * 读到的是**另一个**（截断后的）路径，静默拿到空库/错库；
  * 在磁盘上凭空生成 0 字节垃圾文件；
  * 直接 `unable to open database file`。

现在这些位置统一走 `app.portable_backup._readonly_uri()`（路径经
`quote(safe="/:")` 编码）。本测试锁住该行为。

只用临时目录 / 临时 SQLite 库；不碰生产 `data/agent.db`，不依赖生产库数据，
不新增依赖。末尾以 size + mtime_ns 证明生产库全程未被触碰。

运行：python tests/readonly_uri_test.py
退出码：0 = 全部通过；1 = 有失败。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import _env_guard  # noqa: E402,F401  OPT-04a：数据库隔离闸门（必须在任何 app.* 之前）

from app import portable_backup as pb  # noqa: E402
from app import portable_restore as pr  # noqa: E402

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


def mktmp(prefix="opt01-"):
    p = Path(tempfile.mkdtemp(prefix=prefix))
    _TMPS.append(p)
    return p


# ---------------------------------------------------------------------------
# 造库
# ---------------------------------------------------------------------------
def make_db(path: Path, mark: str = "MARK") -> Path:
    """造一个只有一张表的临时库，用 mark 区分"读到的是不是这个库"。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            "CREATE TABLE t (id INTEGER PRIMARY KEY, mark TEXT NOT NULL);"
        )
        conn.execute("INSERT INTO t (id, mark) VALUES (1, ?)", (mark,))
        conn.commit()
    finally:
        conn.close()
    return path


def read_mark(uri: str) -> str:
    """用给定 URI 只读打开，返回 mark 列；失败抛原始异常。"""
    conn = sqlite3.connect(uri, uri=True)
    try:
        row = conn.execute("SELECT mark FROM t WHERE id = 1").fetchone()
        return "" if row is None else str(row[0])
    finally:
        conn.close()


def dir_listing(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir())


# 可在 Windows 上真实创建的特殊路径（`?` 是 Windows 文件名非法字符，单独处理）
REAL_NAMES = [
    "normal.db",
    "space name.db",
    "记忆数据库.db",
    "hash#1.db",
    "pct%20.db",
    "mixed # % 中文.db",
    "mixed # % ? 中文.db".replace("? ", ""),   # 去掉非法字符，保留 # 与 %
    "amp&1.db",
    "plus+1.db",
    "semi;1.db",
    "at@1.db",
]


# ---------------------------------------------------------------------------
# A. 正确读取
# ---------------------------------------------------------------------------
def test_read_correct():
    print("\n[A] 正确读取")
    for i, name in enumerate(REAL_NAMES):
        d = mktmp()
        dbp = d / name
        mark = f"MARK-{i}-{name}"
        make_db(dbp, mark)
        uri = pb._readonly_uri(dbp)
        try:
            got = read_mark(uri)
            ok, detail = got == mark, f"期望 {mark!r} 实际 {got!r}"
        except sqlite3.Error as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        check(f"A 读取正确 [{name}]", ok, detail)

    # 两个不同库、同名不同内容，不能串味
    d = mktmp()
    a = make_db(d / "a#1.db", "AAA")
    b = make_db(d / "a#2.db", "BBB")
    check("A 不同库不串味 (a#1)", read_mark(pb._readonly_uri(a)) == "AAA")
    check("A 不同库不串味 (a#2)", read_mark(pb._readonly_uri(b)) == "BBB")

    # 子目录里也含特殊字符
    d2 = mktmp()
    deep = d2 / "目录 #1 %2F" / "子目录 #2"
    deep.mkdir(parents=True, exist_ok=True)
    deep_db = make_db(deep / "深#层%.db", "DEEP")
    try:
        got = read_mark(pb._readonly_uri(deep_db))
        check("A 多级特殊字符目录", got == "DEEP", f"实际 {got!r}")
    except sqlite3.Error as e:
        check("A 多级特殊字符目录", False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# B. 只读保护
# ---------------------------------------------------------------------------
def test_readonly_guard():
    print("\n[B] 只读保护")
    for name in ("normal.db", "hash#1.db", "pct%20.db"):
        d = mktmp()
        dbp = make_db(d / name, "GUARD")
        uri = pb._readonly_uri(dbp)
        conn = sqlite3.connect(uri, uri=True)
        try:
            try:
                conn.execute("INSERT INTO t (id, mark) VALUES (2, 'HACK')")
                conn.commit()
                write_blocked = False
            except sqlite3.Error:
                write_blocked = True
        finally:
            conn.close()
        check(f"B 写操作被拒 [{name}]", write_blocked)

        # 内容必须保持原样，且没有第二行
        conn = sqlite3.connect(uri, uri=True)
        try:
            n = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
            mark = conn.execute("SELECT mark FROM t WHERE id=1").fetchone()[0]
        finally:
            conn.close()
        check(f"B 行数未变 [{name}]", n == 1, f"count={n}")
        check(f"B 内容未变 [{name}]", mark == "GUARD", f"mark={mark!r}")

        # 库文件本身大小/mtime 未变
        st1 = dbp.stat()
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("SELECT COUNT(*) FROM t").fetchone()
        conn.close()
        st2 = dbp.stat()
        check(f"B 文件指纹未变 [{name}]",
              (st1.st_size, st1.st_mtime_ns) == (st2.st_size, st2.st_mtime_ns))


# ---------------------------------------------------------------------------
# C. 不存在文件保护（核心：不许凭空造库）
# ---------------------------------------------------------------------------
def test_missing_file():
    print("\n[C] 不存在文件保护")
    for name in ("missing.db", "missing#test.db", "missing%20.db",
                 "缺失 # % 中文.db"):
        d = mktmp()
        target = d / name
        before = dir_listing(d)
        uri = pb._readonly_uri(target)
        try:
            conn = sqlite3.connect(uri, uri=True)
            try:
                conn.execute("PRAGMA user_version").fetchone()
                opened = True
            finally:
                conn.close()
        except sqlite3.Error:
            opened = False
        check(f"C 不存在库只读打开失败 [{name}]", not opened)
        check(f"C 未凭空建文件 [{name}]", dir_listing(d) == before,
              f"{before} -> {dir_listing(d)}")
        check(f"C 目标仍不存在 [{name}]", not target.exists())

    # 整个目录都不存在时，也不能被创建
    d = mktmp()
    missing_dir = d / "不存在的目录 #1"
    target = missing_dir / "x.db"
    before = dir_listing(d)
    try:
        conn = sqlite3.connect(pb._readonly_uri(target), uri=True)
        try:
            conn.execute("PRAGMA user_version").fetchone()
            opened = True
        finally:
            conn.close()
    except sqlite3.Error:
        opened = False
    check("C 目录不存在时打开失败", not opened)
    check("C 目录未被创建", dir_listing(d) == before, f"{dir_listing(d)}")
    check("C 上级目录仍不存在", not missing_dir.exists())


# ---------------------------------------------------------------------------
# D. URI 构造正确性（含 Windows 无法建文件名的 `?`）
# ---------------------------------------------------------------------------
def test_uri_shape():
    print("\n[D] URI 构造")
    d = mktmp()
    dbp = make_db(d / "hash#1.db", "X")
    uri = pb._readonly_uri(dbp)

    check("D 前缀为 file:", uri.startswith("file:"))
    check("D 以 ?mode=ro 结尾", uri.endswith("?mode=ro"), uri)
    check("D 只有一个 ? （路径中的已转义）", uri.count("?") == 1, uri)
    check("D 路径段无裸 #", uri.split("?")[0].count("#") == 0, uri)
    check("D # 已编码为 %23", "%23" in uri, uri)
    check("D 反斜杠已换成正斜杠", "\\" not in uri, uri)
    check("D Windows 路径带前导斜杠", uri.startswith("file:/"), uri)

    # % 必须转义成 %25，否则会被当成转义序列前缀
    pct = mktmp() / "pct%20.db"
    make_db(pct, "PCT")
    uri2 = pb._readonly_uri(pct)
    check("D % 已编码为 %25", "%2520" in uri2 or "%25" in uri2, uri2)
    check("D % 路径可正常读", read_mark(uri2) == "PCT")

    # 空格编码为 %20
    sp = mktmp() / "space name.db"
    make_db(sp, "SP")
    uri3 = pb._readonly_uri(sp)
    check("D 空格编码为 %20", "%20" in uri3, uri3)
    check("D 空格路径可正常读", read_mark(uri3) == "SP")

    # `?` 在 Windows 上不是合法文件名字符，无法建真实文件；
    # 但 URI 构造必须把它转义，否则查询串会被提前截断。
    q_uri = pb._readonly_uri("C:/tmp/query?1.db")
    check("D ? 已编码为 %3F", "%3F" in q_uri, q_uri)
    check("D ? 路径仍只有一个 ?", q_uri.count("?") == 1, q_uri)
    mixed_uri = pb._readonly_uri("C:/tmp/mixed # % ? 中文.db")
    check("D 混合路径 ? 只出现一次", mixed_uri.count("?") == 1, mixed_uri)
    check("D 混合路径 # → %23", "%23" in mixed_uri, mixed_uri)
    check("D 混合路径 % → %25", "%25" in mixed_uri, mixed_uri)
    check("D 混合路径 ? → %3F", "%3F" in mixed_uri, mixed_uri)

    # immutable 变体
    i_uri = pb._readonly_uri(dbp, immutable=True)
    check("D immutable 追加参数", i_uri.endswith("?mode=ro&immutable=1"), i_uri)
    check("D immutable 只读可用", read_mark(i_uri) == "X")


# ---------------------------------------------------------------------------
# E. 反证：未编码的旧写法确实会出错（证明修复是必要的）
# ---------------------------------------------------------------------------
def test_regression_proof():
    print("\n[E] 旧写法反证")
    d = mktmp()
    dbp = make_db(d / "hash#1.db", "PROOF")
    raw_uri = f"file:{dbp}?mode=ro"
    before = dir_listing(d)
    raw_broken = False
    raw_detail = ""
    try:
        got = read_mark(raw_uri)
        # 若"居然"读到了，也必须不是同一个库的数据
        if got != "PROOF":
            raw_broken = True
            raw_detail = f"读到 {got!r}"
    except sqlite3.Error as e:
        raw_broken = True
        raw_detail = f"{type(e).__name__}: {e}"
    check("E 旧写法读不到正确数据（反证）", raw_broken, raw_detail)

    after = dir_listing(d)
    junk = [n for n in after if n not in before and not n.endswith("-wal")
            and not n.endswith("-shm")]
    # 只记录观察值，不做断言：`#` 截断产生的垃圾文件名取决于路径本身，
    # 不是每个平台/路径都会留下（Windows 上通常留下截断名）。
    print(f"  [观察] 旧写法执行后新增文件：{junk}")

    # 新写法同一路径必须成功
    check("E 新写法读得到 (同一路径)", read_mark(pb._readonly_uri(dbp)) == "PROOF")


# ---------------------------------------------------------------------------
# F. 真实调用链：8 个修复点在业务函数里生效
# ---------------------------------------------------------------------------
def test_call_sites():
    print("\n[F] 8 个调用点（经业务函数验证）")
    d = mktmp()
    dbp = make_db(d / "agent #1 %.db", "CALLSITE")
    conn = sqlite3.connect(str(dbp))
    try:
        conn.execute("PRAGMA user_version = 42")
        conn.commit()
    finally:
        conn.close()

    # 1) portable_backup.py:409  _dump_db_logical
    try:
        payload, stats = pb._dump_db_logical(dbp, "full")
        tables = payload.get("tables", {}) if isinstance(payload, dict) else {}
        check("F _dump_db_logical 读到表 t", "t" in tables, f"tables={list(tables)}")
        blob = json.dumps(tables.get("t", {}), ensure_ascii=False)
        check("F _dump_db_logical 读到数据", "CALLSITE" in blob, blob[:200])
    except Exception as e:
        check("F _dump_db_logical 读到表 t", False, f"{type(e).__name__}: {e}")
        check("F _dump_db_logical 读到数据", False, "上一条已失败")

    # 2) portable_backup.read_database_metadata（:272 _readonly_uri）
    try:
        meta = pb.read_database_metadata(dbp)
        check("F read_database_metadata readable", bool(meta.get("readable")), meta.get("error"))
        check("F read_database_metadata user_version==42",
              meta.get("user_version") == 42, meta.get("user_version"))
    except Exception as e:
        check("F read_database_metadata readable", False, f"{type(e).__name__}: {e}")
        check("F read_database_metadata user_version==42", False, "上一条已失败")

    # 3) portable_restore.py:229 _db_fingerprint
    try:
        fp = pr._db_fingerprint(dbp)
        check("F _db_fingerprint exists", fp.get("exists") is True, fp.get("exists"))
        check("F _db_fingerprint user_version==42",
              fp.get("user_version") == 42, fp.get("user_version"))
        check("F _db_fingerprint integrity ok",
              str(fp.get("integrity", "")).lower() == "ok", fp.get("integrity"))
        check("F _db_fingerprint 表计数含 t",
              fp.get("table_counts", {}).get("t") == 1, fp.get("table_counts"))
    except Exception as e:
        for nm in ("exists", "user_version==42", "integrity ok", "表计数含 t"):
            check(f"F _db_fingerprint {nm}", False, f"{type(e).__name__}: {e}")

    # 4) 用含特殊字符的库跑 verify_restored_state（:1323 路径）
    try:
        ver = pr.verify_restored_state("opt01-verify", db_path=dbp, source_root=d)
        ok_shape = isinstance(ver, dict) and bool(ver.get("database_check"))
        check("F verify_restored_state 读到库（不崩、有 database_check）",
              ok_shape, str(ver)[:200] if isinstance(ver, dict) else type(ver))
        check("F verify_restored_state 未把库判成不存在",
              "目标数据库不存在" not in (ver.get("errors") or []),
              str(ver.get("errors"))[:200])
    except Exception as e:
        check("F verify_restored_state 读到库（不崩、有 database_check）", False,
              f"{type(e).__name__}: {e}")
        check("F verify_restored_state 未把库判成不存在", False, "上一条已失败")

    # 5) 临时目录说明：本组只在 mktmp() 里工作，不写别处
    d3 = mktmp()
    check("F 新建临时目录初始为空", dir_listing(d3) == [], f"{dir_listing(d3)}")


# ---------------------------------------------------------------------------
# G. read_database_metadata 在 WAL 库上的行为不因编码而变
# ---------------------------------------------------------------------------
def test_wal_still_works():
    print("\n[G] WAL 库")
    d = mktmp()
    dbp = d / "wal #1.db"
    conn = sqlite3.connect(str(dbp))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, mark TEXT NOT NULL)")
        conn.execute("INSERT INTO t (id, mark) VALUES (1, 'WAL')")
        conn.commit()
    finally:
        conn.close()
    meta = pb.read_database_metadata(dbp)
    check("G WAL 库 readable", bool(meta.get("readable")), meta.get("error"))
    check("G WAL 库读到 embedding 不崩", isinstance(meta.get("embedding"), dict))
    check("G WAL 库特殊路径可读", read_mark(pb._readonly_uri(dbp)) == "WAL")


# ---------------------------------------------------------------------------
def main() -> int:
    print("=== OPT-01 SQLite 只读 URI 编码测试 ===")
    prod = Path(pb.db.DB_PATH)
    prod_before = (prod.stat().st_size, prod.stat().st_mtime_ns) if prod.is_file() else None

    try:
        test_read_correct()
        test_readonly_guard()
        test_missing_file()
        test_uri_shape()
        test_regression_proof()
        test_call_sites()
        test_wal_still_works()
    finally:
        for p in _TMPS:
            shutil.rmtree(p, ignore_errors=True)

    prod_after = (prod.stat().st_size, prod.stat().st_mtime_ns) if prod.is_file() else None
    check("生产库指纹全程未变（size + mtime_ns）", prod_before == prod_after,
          f"{prod_before} -> {prod_after}")

    print(f"\n=== 结果：{PASS} PASS / {FAIL} FAIL ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
