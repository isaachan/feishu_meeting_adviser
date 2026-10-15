"""Architecture ratchets for boundaries that are intentionally migrated in stages."""
import ast
from collections import Counter
from pathlib import Path
import re
import unittest


PACKAGE = Path(__file__).parents[1] / "src" / "feishu_meeting_adviser"
SQL_START = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|PRAGMA|REPLACE|VACUUM)\b", re.I)
SQL_TABLE = re.compile(
    r"\b(?:FROM|INTO|UPDATE|TABLE|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.I,
)
IMPLICIT_INSERT = re.compile(
    r"\bINSERT\s+(?:OR\s+[A-Z]+\s+)?INTO\s+[A-Za-z_][A-Za-z0-9_]*\s+(?:VALUES|SELECT|DEFAULT)\b",
    re.I,
)

# These are pre-existing boundary violations, identified by stable scope and SQL intent.
# Every entry must carry a migration reason; the set may shrink but must never grow.
DIRECT_SQL_ALLOWLIST = {
    ("__main__.py", "main", "SELECT", "tombstones"): "stage C1 source query API",
    ("__main__.py", "main", "SELECT", "conflicts"): "stage C1 conflict query API",
    ("adviser.py", "AdviserRunner._ensure_materials", "UPDATE", "meetings"): "stage C1 material state API",
    ("adviser.py", "AdviserRunner.guard", "SELECT", "tombstones"): "stage C1 deletion query API",
    ("maintenance.py", "Maintenance._clean_consumer_copies", "SELECT", "tombstones"): "stage C1 deletion query API",
    ("maintenance.py", "Maintenance._clean_consumer_copies", "SELECT", "meetings"): "stage C1 meeting query API",
    ("reply_sender.py", "introduce", "SELECT", "tombstones"): "stage C1 deletion query API",
}


def _scope(parents):
    names = [node.name for node in parents if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
    return ".".join(names) or "<module>"


def _static_sql(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else "?" for value in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _static_sql(node.left), _static_sql(node.right)
        return left + right if left is not None and right is not None else None
    return None


def _uses_db_attribute(node):
    while isinstance(node, ast.Attribute):
        if node.attr == "db":
            return True
        node = node.value
    return False


def _sql_signature(path, node, parents):
    if not isinstance(node.func, ast.Attribute) or node.func.attr not in {"execute", "executemany", "executescript"}:
        return None
    if not node.args:
        return (path.name, _scope(parents), "DB_ACCESS", node.func.attr) if _uses_db_attribute(node.func) else None
    sql = _static_sql(node.args[0])
    if sql is None:
        return (path.name, _scope(parents), "DB_ACCESS", node.func.attr) if _uses_db_attribute(node.func) else None
    if not isinstance(sql, str):
        return None
    operation = SQL_START.match(sql)
    if not operation:
        return None
    table = SQL_TABLE.search(sql)
    return (path.name, _scope(parents), operation.group(1).upper(), table.group(1).lower() if table else "<none>")


def direct_sql_violations():
    violations = Counter()
    for path in PACKAGE.rglob("*.py"):
        relative = path.relative_to(PACKAGE)
        if path.name in {"storage.py", "adviser_store.py"} or "persistence" in relative.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = []

        def visit(node):
            signature = _sql_signature(path, node, parents) if isinstance(node, ast.Call) else None
            if signature:
                violations[signature] += 1
            parents.append(node)
            for child in ast.iter_child_nodes(node):
                visit(child)
            parents.pop()

        visit(tree)
    return violations


def implicit_column_inserts():
    violations = []
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            sql = _static_sql(node.args[0])
            if isinstance(sql, str) and IMPLICIT_INSERT.search(sql):
                violations.append((str(path.relative_to(PACKAGE)), node.lineno))
    return violations


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_product_direct_sql_matches_shrinking_allowlist(self):
        actual = direct_sql_violations()
        expected = Counter(DIRECT_SQL_ALLOWLIST.keys())
        self.assertEqual(
            actual,
            expected,
            "Direct product SQL changed. New entries are forbidden; remove migrated entries from the allowlist.",
        )

    def test_product_inserts_always_name_columns(self):
        self.assertEqual(
            implicit_column_inserts(),
            [],
            "Product INSERT statements must name columns so schema evolution cannot reorder values.",
        )


if __name__ == "__main__":
    unittest.main()
