"""Finite, conditional values for an original frozen safe YAML read.

No YAML package or analyzed source is imported or executed. The independent
parser covers only the stated block subset of the reviewed PyYAML reader.
Code identity is an observation; original runtime tables, bytes at read time
and read/close failures remain explicit conditions, never SDK permissions.
"""

import ast
import hashlib
import importlib.metadata
import math
import posixpath
import re
import sys
from pathlib import Path, PurePosixPath


RULE = "source-frozen-yaml-values/1"
# Reviewed pure-Python safe_load -> load -> SafeLoader reader closure, 6.0.3.
# These hashes identify the required code; the finite grammar below, not a
# package name or matching hash, defines the supported result domain.
READER_FILES = {
    "__init__.py": "b19dfcc333d6a75dfd73073901164507252f271b41d3b5f7d85510033a0547a7",
    "loader.py": "5156becc8aa6905482218abf3e04869b835226db4763645fff3438fdbd5f1cdd",
    "reader.py": "d1d9b38ab3a20c6e17a38d519ee412ecaf6b918df18c78956ac7c330d4ea08dc",
    "scanner.py": "60433788b652690c17710460da5d91e0c753d3318fd85f5e1e42862a71f25906",
    "parser.py": "8a55a9e6fbe0a07146cef3990c8b45a068c3e83e369e1959ad9ca30306b4a09a",
    "composer.py": "fcaa37d16afa783594794a5ab94193dcb720f503c19ce3d59539c8311189f453",
    "constructor.py": "90d8247da78b524c10618fd0e857f54f3d97570fe91b5c5513d024ef3faf88b0",
    "resolver.py": "f4bf9561f9b89961f1503d558385fbae30d12bfed565de9bf76c33abb63620a6",
    "error.py": "021f73fada072546c4f63f8cf18a7181244ce4280b09cc15cc980b2d1176171a",
    "tokens.py": "953408cd2570f0c83dc2fe39f7e4e388e41eeb05738aa69196a5f6ffcf6ba79e",
    "events.py": "e74fd392c810884e2ea7e94aa3f57e9c1cbeb402319083d0c58e6a0e1282787c",
    "nodes.py": "80f28d8fca4a09d87677882bde021820d9cf39a3b11a12405226211919cf13ce",
}


class ConfigurationUnknown(ValueError):
    pass


def reader_source_check(inspector):
    """Inspect finite direct imports/aliases; never certify runtime immutability.

    YAML-origin references outside plain aliases and direct safe_load calls are
    unsupported, including container/destructuring/dynamic/return escapes.
    Third-party imports and the original runtime tables remain UNKNOWN even
    when every checked frozen source use is within these finite forms.
    """
    facts = dict(status="FINITE_FORMS_CHECKED", runtime_state="UNKNOWN",
                 coverage="direct imports, plain name/attribute aliases and direct safe_load calls only")
    def fail(path, node, reason):
        return dict(facts, status="UNKNOWN", reason=reason, path=path,
                    line=getattr(node, "lineno", None))
    for path in inspector.files:
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(inspector.data(path))
        except (SyntaxError, ValueError, KeyError, OSError):
            return fail(path, None, "frozen source is unavailable for the finite reader check")
        aliases = {}
        def add(key, values):
            before = aliases.get(key, set())
            aliases[key] = before | values
            return before != aliases[key]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    if item.name == "yaml" or item.name.startswith("yaml."):
                        add(item.asname or item.name.split(".")[0], {item.name if item.asname else "yaml"})
            if isinstance(node, ast.ImportFrom) and not node.level and (node.module or "").split(".")[0] == "yaml":
                for item in node.names:
                    add(item.asname or item.name, {node.module + "." + item.name})
        def names(node):
            if isinstance(node, ast.Name):
                return aliases.get(node.id, set())
            if isinstance(node, ast.Attribute):
                return {parent + "." + node.attr for parent in names(node.value)}
            if isinstance(node, ast.Subscript):
                return names(node.value)
            return set()
        nodes = list(ast.walk(tree))
        for _ in range(32):
            changed = False
            for node in nodes:
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    changed |= add(node.targets[0].id, names(node.value))
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    changed |= add(node.target.id, names(node.value))
            if not changed:
                break
        else:
            return fail(path, tree, "finite reader alias fixed-point bound exceeded")
        parents = {id(child): node for node in nodes for child in ast.iter_child_nodes(node)}
        for node in nodes:
            targets = (node.targets if isinstance(node, (ast.Assign, ast.Delete)) else
                       [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else [])
            if any(isinstance(target, (ast.Attribute, ast.Subscript)) and names(target) for target in targets):
                return fail(path, node, "explicit write to a YAML-origin object")
            if not isinstance(node, (ast.Name, ast.Attribute)) or not names(node):
                continue
            if isinstance(getattr(node, "ctx", None), (ast.Store, ast.Del)):
                continue
            parent = parents.get(id(node))
            if isinstance(parent, ast.Attribute) and parent.value is node:
                continue  # Check only the complete attribute expression.
            if isinstance(parent, ast.Call) and parent.func is node and names(node) == {"yaml.safe_load"}:
                continue
            if (isinstance(parent, ast.Assign) and parent.value is node and len(parent.targets) == 1
                    and isinstance(parent.targets[0], ast.Name) or isinstance(parent, ast.AnnAssign)
                    and parent.value is node and isinstance(parent.target, ast.Name)):
                continue
            return fail(path, node, "YAML-origin reference/call is outside the finite alias and safe_load forms")
    return facts


def reader_identity(directory=None):
    """Read installed source bytes only, independently of loaded module state."""
    observation = dict(required_reader="PyYAML.safe_load/SafeLoader pure Python 6.0.3",
        required_files=dict(READER_FILES), installed_source="UNKNOWN",
        loaded="PRESENT_UNVERIFIED" if "yaml" in sys.modules else "NOT_LOADED",
        runtime_tables="UNKNOWN", target_runtime="NOT_PROVED")
    try:
        folder = Path(directory) if directory is not None else Path(
            importlib.metadata.distribution("PyYAML").locate_file("yaml"))
        actual = {}
        for name in READER_FILES:
            path = folder / name
            if path.is_symlink() or not path.is_file():
                raise ConfigurationUnknown("reader source file is absent or a symlink")
            actual[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        observation["observed_files"] = actual
        observation["installed_source"] = "MATCHED" if actual == READER_FILES else "MISMATCH"
    except (OSError, ValueError, importlib.metadata.PackageNotFoundError):
        pass
    return observation


def plain_yaml_value(data):
    """ASCII block maps/sequences, str/bool/null/decimal int/finite decimal float.

    String keys are unique identifiers, excluding implicit scalar keywords.
    Plain string leaves contain only letters, digits, _, ., / and -, and start
    with a letter/underscore. Compact sequence maps and flat flow sequences of
    these scalars retain their original order. Quoting, flow maps, nested flow,
    tags, references, directives, documents and multiline forms are refused.
    Empty mapping values and dash-only leaves have original YAML null value.
    """
    if type(data) is not bytes or len(data) > 65536:
        raise ConfigurationUnknown("configuration byte bound or type is unsupported")
    try:
        source = data.decode("ascii")
    except UnicodeError as error:
        raise ConfigurationUnknown("configuration needs the finite ASCII subset") from error
    if any(ord(c) < 32 and c not in "\r\n" for c in source):
        raise ConfigurationUnknown("configuration contains unsupported whitespace/control bytes")
    lines = []
    for number, line in enumerate(source.splitlines(), 1):
        if number > 2048:
            raise ConfigurationUnknown("configuration line bound exceeded")
        line = re.split(r"\s+#", line, maxsplit=1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        text = line[indent:]
        compact = re.fullmatch(r"- ([A-Za-z_][A-Za-z_0-9]*):(?: (.*))?", text)
        if compact is not None:
            # An internal syntax normalization only: the original bytes,
            # original read and read/close effects are never changed.
            lines.append((indent, '-'))
            lines.append((indent + 2, text[2:]))
        else:
            lines.append((indent, text))
    if not lines or lines[0][0] != 0:
        raise ConfigurationUnknown("configuration root must be a nonempty block at column zero")
    nodes = 0
    def scalar(text, flow_item=False):
        nonlocal nodes
        nodes += 1
        if nodes > 1024:
            raise ConfigurationUnknown("configuration value bound exceeded")
        if not flow_item and text.startswith('[') and text.endswith(']'):
            content = text[1:-1].strip()
            if not content:
                return []
            if content.endswith(','):
                content = content[:-1].rstrip()
            parts = [part.strip() for part in content.split(',')]
            if not all(parts):
                raise ConfigurationUnknown('configuration flow sequence has an uncovered empty entry')
            return [scalar(part, flow_item=True) for part in parts]
        if text in {"", "~", "null", "Null", "NULL"}:
            return None
        if text in {"true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"}:
            return True
        if text in {"false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"}:
            return False
        if re.fullmatch(r"-?(?:0|[1-9][0-9]{0,63})", text):
            return int(text)
        if re.fullmatch(r"-?(?:0|[1-9][0-9]{0,63})\.[0-9]{1,64}", text):
            value = float(text)
            if math.isfinite(value):
                return value
        if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9./-]*", text):
            return text
        raise ConfigurationUnknown("configuration scalar is outside the reviewed ordinary subset")
    def block(index, indent, depth):
        if depth > 12:
            raise ConfigurationUnknown("configuration nesting bound exceeded")
        sequence = lines[index][1] == "-" or lines[index][1].startswith("- ")
        output = [] if sequence else {}
        while index < len(lines) and lines[index][0] == indent:
            text = lines[index][1]
            is_item = text == "-" or text.startswith("- ")
            if is_item != sequence:
                break  # An indentless mapping-owned sequence may end here.
            if sequence:
                tail = text[1:].strip()
                key = None
            else:
                match = re.fullmatch(r"([A-Za-z_][A-Za-z_0-9]*):(?: (.*))?", text)
                if match is None:
                    raise ConfigurationUnknown("configuration needs unambiguous block mapping keys")
                key, tail = match[1], match[2] or ""
                if type(scalar(key)) is not str or key in output:
                    raise ConfigurationUnknown("configuration key is ambiguous or duplicated")
            index += 1
            if tail:
                value = scalar(tail)
                if index < len(lines) and lines[index][0] > indent:
                    raise ConfigurationUnknown("a scalar cannot own nested configuration")
            elif index < len(lines) and (lines[index][0] > indent or
                    not sequence and lines[index][0] == indent and
                    (lines[index][1] == "-" or lines[index][1].startswith("- "))):
                value, index = block(index, lines[index][0], depth + 1)
            else:
                value = scalar("")
            if sequence:
                output.append(value)
            else:
                output[key] = value
        return output, index
    value, end = block(0, 0, 0)
    if end != len(lines):
        raise ConfigurationUnknown("configuration indentation or block kind is unsupported")
    return value


def frozen_bytes(inspector, raw_path):
    """The original path must stay in one frozen root without symlink traversal."""
    if type(raw_path) is not str or "\\" in raw_path or "\x00" in raw_path:
        raise ConfigurationUnknown("configuration path is not a known frozen POSIX path")
    path = PurePosixPath(raw_path)
    if path.is_absolute():
        raise ConfigurationUnknown("external configuration path is not covered")
    current = inspector.root
    for index, part in enumerate(path.parts):
        current = current / part
        if current.is_symlink():
            raise ConfigurationUnknown("configuration path traverses a symlink")
        if not current.resolve().is_relative_to(inspector.root):
            raise ConfigurationUnknown("configuration path leaves the frozen root")
        if index < len(path.parts) - 1 and not current.is_dir():
            raise ConfigurationUnknown("configuration path has no original directory prefix")
    normalized = posixpath.normpath(raw_path)
    if normalized not in inspector.files or not normalized.endswith((".yaml", ".yml")):
        raise ConfigurationUnknown("configuration file is absent from this frozen source")
    data = inspector.data(normalized)
    record = inspector.files[normalized]
    if (type(data) is not bytes or hashlib.sha256(data).hexdigest() != record.get("sha256")
            or len(data) != record.get("bytes")):
        raise ConfigurationUnknown("configuration frozen content identity changed")
    return normalized, data
