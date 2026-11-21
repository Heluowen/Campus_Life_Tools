"""Minimal Java Object Serialization Stream reader for Charles .chls files.

Self-contained implementation of the subset of the Java serialization
protocol needed to reconstruct plain-POJO object graphs (Charles model
classes are ordinary Serializable POJOs).

Usage:
    python chls_dump.py <file.chls> [--json out.json]
"""
from __future__ import annotations

import io
import json
import struct
import sys
import zlib

STREAM_MAGIC = 0xACED
STREAM_VERSION = 5
BASE_HANDLE = 0x7E0000

TC_NULL = 0x70
TC_REFERENCE = 0x71
TC_CLASSDESC = 0x72
TC_OBJECT = 0x73
TC_STRING = 0x74
TC_ARRAY = 0x75
TC_CLASS = 0x76
TC_BLOCKDATA = 0x77
TC_ENDBLOCKDATA = 0x78
TC_RESET = 0x79
TC_BLOCKDATALONG = 0x7A
TC_EXCEPTION = 0x7B
TC_LONGSTRING = 0x7C
TC_PROXYCLASSDESC = 0x7D
TC_ENUM = 0x7E

SC_SERIALIZABLE = 0x02
SC_WRITE_METHOD = 0x01
SC_EXTERNALIZABLE = 0x04
SC_BLOCK_DATA = 0x08
SC_ENUM = 0x10


class EndBlock:
    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst


ENDBLOCK = EndBlock()
RESET = object()


class BlockData:
    __slots__ = ("data",)

    def __init__(self, data: bytes):
        self.data = data

    def __repr__(self):
        return f"BlockData({len(self.data)}B)"

    def text(self) -> str:
        try:
            return self.data.decode("utf-8", errors="replace")
        except Exception:
            return self.data.hex()


class ClassDesc:
    __slots__ = ("name", "suid", "flags", "fields", "super_desc", "annotations", "handle")

    def __init__(self):
        self.name = ""
        self.suid = 0
        self.flags = 0
        self.fields = []  # (typecode:int, name:str, classname:str|None)
        self.super_desc = None
        self.annotations = []
        self.handle = None

    def __repr__(self):
        return f"ClassDesc({self.name}, flags={self.flags:#x})"


class JavaObject:
    __slots__ = ("cls", "desc", "fields", "annotations", "handle")

    def __init__(self, cls: str, desc=None):
        self.cls = cls
        self.desc = desc
        self.fields = {}
        self.annotations = []
        self.handle = None

    def __repr__(self):
        return f"JavaObject({self.cls})"


class JavaString:
    __slots__ = ("value", "handle")

    def __init__(self, value: str):
        self.value = value
        self.handle = None

    def __repr__(self):
        return f"JavaString({self.value!r})"


class JavaArray:
    __slots__ = ("desc_name", "values", "handle")

    def __init__(self, desc_name: str, values):
        self.desc_name = desc_name
        self.values = values
        self.handle = None

    def __repr__(self):
        return f"JavaArray({self.desc_name}, n={len(self.values)})"


class JavaEnum:
    __slots__ = ("desc_name", "constant", "handle")

    def __init__(self, desc_name, constant):
        self.desc_name = desc_name
        self.constant = constant
        self.handle = None

    def __repr__(self):
        return f"JavaEnum({self.desc_name}.{self.constant})"


def decode_modified_utf8(b: bytes) -> str:
    # Java modified UTF-8: NUL is encoded as C0 80; surrogate pairs allowed.
    if b"\xc0\x80" in b:
        b = b.replace(b"\xc0\x80", b"\x00")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("utf-8", errors="replace")


class Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.handles: list = []

    # -- primitive readers --
    def _need(self, n: int):
        if self.pos + n > len(self.data):
            raise EOFError(f"unexpected end of stream at {self.pos}")

    def u1(self) -> int:
        self._need(1)
        v = self.data[self.pos]
        self.pos += 1
        return v

    def i1(self) -> int:
        v = self.u1()
        return v - 256 if v >= 128 else v

    def u2(self) -> int:
        self._need(2)
        v = struct.unpack_from(">H", self.data, self.pos)[0]
        self.pos += 2
        return v

    def i2(self) -> int:
        v = self.u2()
        return v - 65536 if v >= 32768 else v

    def i4(self) -> int:
        self._need(4)
        v = struct.unpack_from(">i", self.data, self.pos)[0]
        self.pos += 4
        return v

    def u4(self) -> int:
        self._need(4)
        v = struct.unpack_from(">I", self.data, self.pos)[0]
        self.pos += 4
        return v

    def i8(self) -> int:
        self._need(8)
        v = struct.unpack_from(">q", self.data, self.pos)[0]
        self.pos += 8
        return v

    def f4(self) -> float:
        self._need(4)
        v = struct.unpack_from(">f", self.data, self.pos)[0]
        self.pos += 4
        return v

    def f8(self) -> float:
        self._need(8)
        v = struct.unpack_from(">d", self.data, self.pos)[0]
        self.pos += 8
        return v

    def raw(self, n: int) -> bytes:
        self._need(n)
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def utf(self) -> str:
        n = self.u2()
        return decode_modified_utf8(self.raw(n))

    def read_string_value(self):
        """Read a string object (TC_STRING / TC_LONGSTRING / TC_REFERENCE)."""
        v = self.read_object_token()
        if isinstance(v, JavaString):
            return v.value
        if v is None:
            return None
        return str(v)

    def register(self, obj):
        """Assign the next handle to obj (handles start at BASE_HANDLE)."""
        obj.handle = BASE_HANDLE + len(self.handles)
        self.handles.append(obj)
        return obj

    # -- structural readers --
    def read_class_desc(self) -> ClassDesc:
        desc = self.register(ClassDesc())  # register before filling
        desc.name = self.utf()
        desc.suid = self.i8()
        desc.flags = self.u1()
        n = self.u2()
        desc.fields = []
        for _ in range(n):
            t = self.u1()
            fname = self.utf()
            cname = None
            if t in (ord("L"), ord("[")):
                cname = self.read_string_value()
            desc.fields.append((t, fname, cname))
        # class annotations
        desc.annotations = self.read_annotations()
        super_desc = self.read_class_desc_or_null()
        desc.super_desc = super_desc
        return desc

    def read_class_desc_or_null(self):
        tag = self.u1()
        if tag == TC_NULL:
            return None
        if tag == TC_REFERENCE:
            h = self.i4()
            return self.lookup(h)
        if tag == TC_CLASSDESC:
            return self.read_class_desc()
        raise ValueError(f"expected classdesc, got tag {tag:#x}")

    def read_annotations(self) -> list:
        out = []
        while True:
            v = self.read_object_token()
            if v is ENDBLOCK:
                return out
            if v is RESET:
                continue
            out.append(v)

    def lookup(self, handle: int):
        idx = handle - BASE_HANDLE
        if idx < 0 or idx >= len(self.handles):
            raise ValueError(f"bad handle {handle:#x}")
        return self.handles[idx]

    def read_object_token(self):
        tag = self.u1()
        if tag == TC_NULL:
            return None
        if tag == TC_REFERENCE:
            return self.lookup(self.i4())
        if tag == TC_CLASSDESC:
            return self.read_class_desc()
        if tag == TC_OBJECT:
            return self.read_new_object()
        if tag == TC_STRING:
            return self.register(JavaString(self.utf()))
        if tag == TC_LONGSTRING:
            n = self.i8()
            return self.register(JavaString(decode_modified_utf8(self.raw(n))))
        if tag == TC_ARRAY:
            return self.read_new_array()
        if tag == TC_CLASS:
            return self.read_object_token()  # classdesc
        if tag == TC_BLOCKDATA:
            n = self.u1()
            return BlockData(self.raw(n))
        if tag == TC_BLOCKDATALONG:
            n = self.u4()
            return BlockData(self.raw(n))
        if tag == TC_ENDBLOCKDATA:
            return ENDBLOCK
        if tag == TC_RESET:
            return RESET
        if tag == TC_PROXYCLASSDESC:
            return self.read_proxy_classdesc()
        if tag == TC_ENUM:
            return self.read_enum()
        if tag == TC_EXCEPTION:
            return self.read_object_token()  # exception object, treat as value
        raise ValueError(f"unsupported tag {tag:#x} at pos {self.pos - 1}")

    def read_proxy_classdesc(self):
        desc = self.register(ClassDesc())
        desc.name = "<proxy>"
        desc.suid = 0
        n = self.i4()
        desc.fields = []
        desc.annotations = self.read_annotations()
        desc.super_desc = self.read_class_desc_or_null()
        return desc

    def read_enum(self):
        desc = self.read_object_token()
        name = desc.name if isinstance(desc, ClassDesc) else "?"
        constant = self.utf()
        return self.register(JavaEnum(name, constant))

    def read_new_object(self) -> JavaObject:
        desc = self.read_object_token()
        cls = desc.name if isinstance(desc, ClassDesc) else "?"
        obj = self.register(JavaObject(cls, desc))
        self.read_classdata(desc, obj)
        return obj

    def read_new_array(self) -> JavaArray:
        desc = self.read_object_token()
        name = desc.name if isinstance(desc, ClassDesc) else "[?"
        size = self.i4()
        if len(name) == 2 and name[0] == "[":
            t = name[1]
            values = self.read_primitive_array(t, size)
        else:
            values = [self.read_object_token() for _ in range(size)]
        arr = self.register(JavaArray(name, values))
        return arr

    def read_primitive_array(self, t: str, size: int):
        if t == "B":
            return list(self.raw(size))
        if t == "Z":
            return [bool(b) for b in self.raw(size)]
        if t == "C":
            return list(self.raw(size * 2).decode("utf-16-be", errors="replace"))
        if t == "S":
            self._need(size * 2)
            vals = list(struct.unpack_from(f">{size}h", self.data, self.pos))
            self.pos += size * 2
            return vals
        if t == "I":
            self._need(size * 4)
            vals = list(struct.unpack_from(f">{size}i", self.data, self.pos))
            self.pos += size * 4
            return vals
        if t == "J":
            self._need(size * 8)
            vals = list(struct.unpack_from(f">{size}q", self.data, self.pos))
            self.pos += size * 8
            return vals
        if t == "F":
            self._need(size * 4)
            vals = list(struct.unpack_from(f">{size}f", self.data, self.pos))
            self.pos += size * 4
            return vals
        if t == "D":
            self._need(size * 8)
            vals = list(struct.unpack_from(f">{size}d", self.data, self.pos))
            self.pos += size * 8
            return vals
        raise ValueError(f"bad primitive array type {t!r}")

    def read_classdata(self, desc: ClassDesc, obj: JavaObject):
        if not isinstance(desc, ClassDesc):
            return
        if desc.super_desc is not None:
            self.read_classdata(desc.super_desc, obj)
        flags = desc.flags
        if flags & SC_EXTERNALIZABLE:
            if flags & SC_BLOCK_DATA:
                obj.annotations.append(BlockData(self.read_blockdata_until_end()))
            else:
                raise NotImplementedError("externalizable without blockdata")
            return
        if not (flags & SC_SERIALIZABLE):
            return
        # 字段数据总是先写入（writeObject 内部通常先调 defaultWriteObject）
        for t, fname, fclass in desc.fields:
            obj.fields[fname] = self.read_value(t, fclass)
        # 自定义 writeObject 的注解内容（blockdata / 对象引用）直到 ENDBLOCKDATA
        if flags & SC_WRITE_METHOD:
            obj.annotations.extend(self.read_annotations())

    def read_value(self, t: int, fclass):
        if t == ord("B"):
            return self.i1()
        if t == ord("C"):
            return chr(self.u2())
        if t == ord("D"):
            return self.f8()
        if t == ord("F"):
            return self.f4()
        if t == ord("I"):
            return self.i4()
        if t == ord("J"):
            return self.i8()
        if t == ord("S"):
            return self.i2()
        if t == ord("Z"):
            return bool(self.i1())
        if t in (ord("L"), ord("[")):
            return self.read_object_token()
        raise ValueError(f"unknown field typecode {t:#x}")

    def read_blockdata_until_end(self) -> bytes:
        chunks = []
        while True:
            tag = self.u1()
            if tag == TC_ENDBLOCKDATA:
                return b"".join(chunks)
            if tag == TC_BLOCKDATA:
                n = self.u1()
                chunks.append(self.raw(n))
            elif tag == TC_BLOCKDATALONG:
                n = self.u4()
                chunks.append(self.raw(n))
            elif tag == TC_REFERENCE:
                self.i4()  # ignore nonserializable annotations referencing handles
            elif tag == TC_NULL:
                pass
            elif tag in (TC_STRING, TC_LONGSTRING):
                pass  # strings in block data are unusual; skip
            else:
                raise ValueError(f"unexpected tag in blockdata: {tag:#x}")

    def read_top(self) -> list:
        magic = self.u2()
        version = self.u2()
        if magic != STREAM_MAGIC or version != STREAM_VERSION:
            raise ValueError(f"bad stream header {magic:#x}/{version}")
        objs = []
        while self.pos < len(self.data):
            v = self.read_object_token()
            if v is RESET or v is ENDBLOCK:
                continue
            objs.append(v)
            self._maybe_reset()
        return objs

    def _maybe_reset(self):
        pass

    def dump_json(self, objs) -> str:
        return json.dumps(plain(objs), ensure_ascii=False, default=str)


def plain(v, depth=0, max_depth=20, seen=None):
    if seen is None:
        seen = set()
    if depth > max_depth:
        return "<max-depth>"
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, bytes):
        return f"<{len(v)}B>"
    oid = id(v)
    if oid in seen:
        return "<cycle>"
    seen = seen | {oid}
    if isinstance(v, JavaString):
        return v.value
    if isinstance(v, JavaObject):
        out = {"__class__": v.cls}
        if v.fields:
            out["fields"] = {k: plain(x, depth + 1, max_depth, seen) for k, x in v.fields.items()}
        if v.annotations:
            out["annotations"] = [plain(x, depth + 1, max_depth, seen) for x in v.annotations]
        return out
    if isinstance(v, JavaArray):
        return [plain(x, depth + 1, max_depth, seen) for x in v.values]
    if isinstance(v, BlockData):
        return v.text()
    if isinstance(v, JavaEnum):
        return f"{v.desc_name}.{v.constant}"
    return repr(v)


def summary(v, depth=0, max_depth=12, seen=None, out=None):
    """Compact human-readable tree dump."""
    if out is None:
        out = []
    if seen is None:
        seen = set()
    pad = "  " * depth
    if depth > max_depth:
        out.append(f"{pad}…")
        return out
    if isinstance(v, JavaString):
        out.append(f"{pad}str {v.value!r}")
        return out
    if isinstance(v, BlockData):
        txt = v.text()
        if len(txt) > 120:
            txt = txt[:120] + f"...({len(v.data)}B)"
        out.append(f"{pad}bytes: {txt!r}")
        return out
    oid = id(v)
    if oid in seen:
        out.append(f"{pad}<ref {type(v).__name__}>")
        return out
    seen = seen | {oid}
    if isinstance(v, JavaObject):
        out.append(f"{pad}{v.cls}")
        for k, x in v.fields.items():
            if isinstance(x, (str, int, float, bool)) or x is None:
                s = repr(x)
                if len(s) > 100:
                    s = s[:100] + "…"
                out.append(f"{pad}  {k} = {s}")
            else:
                out.append(f"{pad}  {k}:")
                summary(x, depth + 2, max_depth, seen, out)
        for i, x in enumerate(v.annotations):
            if isinstance(x, BlockData):
                continue
            out.append(f"{pad}  ann{i}:")
            summary(x, depth + 2, max_depth, seen, out)
        return out
    if isinstance(v, JavaArray):
        out.append(f"{pad}array {v.desc_name} n={len(v.values)}")
        for i, x in enumerate(v.values[:20]):
            out.append(f"{pad} [{i}]:")
            summary(x, depth + 2, max_depth, seen, out)
        if len(v.values) > 20:
            out.append(f"{pad} … {len(v.values) - 20} more")
        return out
    if isinstance(v, list):
        out.append(f"{pad}list n={len(v)}")
        for x in v[:20]:
            summary(x, depth + 2, max_depth, seen, out)
        return out
    out.append(f"{pad}{type(v).__name__}: {v!r}")
    return out


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    path = argv[1]
    with open(path, "rb") as f:
        data = f.read()
    reader = Reader(data)
    objs = reader.read_top()
    print(f"stream objects: {len(objs)}  handles: {len(reader.handles)}")
    for i, o in enumerate(objs):
        print(f"=== top object #{i} ===")
        for line in summary(o):
            print(line)
    if "--json" in argv:
        idx = argv.index("--json")
        out_path = argv[idx + 1] if idx + 1 < len(argv) else "chls_dump.json"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(plain(objs).__repr__() if False else json.dumps(plain(objs), ensure_ascii=False, default=str))
        print(f"json written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
