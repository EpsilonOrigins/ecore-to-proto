# Ecore to Protocol Buffers Converter

A Python tool that converts Eclipse EMF Ecore (`.ecore`) model files into Protocol Buffers v3 (`.proto`) files. Designed to handle collections of interdependent Ecore files with full cross-reference resolution.

## Features

- **Multi-file support** — pass multiple `.ecore` files or an entire directory; cross-package references are resolved automatically
- **Inheritance via composition** — Ecore `eSuperTypes` are mapped to embedded parent message fields (proto3 has no inheritance)
- **Enum generation** — Ecore `EEnum` types become proto3 enums with prefixed `UPPER_SNAKE_CASE` values and a guaranteed zero-value entry
- **Type mapping** — built-in Ecore primitives (`EString`, `EInt`, `EDate`, etc.) and Java instance class names map to appropriate proto scalar types
- **Repeated fields** — `upperBound="-1"` attributes and references become `repeated` fields
- **Well-known type imports** — `EDate` maps to `google.protobuf.Timestamp`, with the correct import generated automatically
- **Cross-package imports** — references between packages produce the correct `import` and qualified type names
- **Metadata preservation** — containment, opposites, abstract markers, and default values are retained as comments
- **Sub-package flattening** — nested `eSubpackages` each produce their own `.proto` file

## Requirements

Python 3.8+ (standard library only — no external dependencies).

## Usage

```bash
# Single file
python ecore_to_proto.py model.ecore

# Multiple interdependent files
python ecore_to_proto.py base.ecore domain.ecore extensions.ecore -o proto_out/

# Entire directory (recursive search for *.ecore)
python ecore_to_proto.py ./ecore_models/ -o proto_out/ -v

# With language-specific package options
python ecore_to_proto.py ./models/ -o output/ \
    --java-package com.example \
    --go-package github.com/org/pkg \
    --proto-package com.example
```

### CLI Options

| Flag | Description |
|---|---|
| `inputs` | One or more `.ecore` files or directories (positional) |
| `-o`, `--output-dir` | Output directory for generated `.proto` files (default: `.`) |
| `--java-package` | Java package prefix added as `option java_package` |
| `--go-package` | Go package prefix added as `option go_package` |
| `--proto-package` | Prefix prepended to the `package` declaration |
| `-v`, `--verbose` | Print parsing and generation details to stderr |

### Programmatic Use

```python
from ecore_to_proto import convert

proto_files = convert(
    ecore_files=["base.ecore", "domain.ecore"],
    output_dir="proto_out/",
    java_package="com.example",
    verbose=True,
)
# proto_files is a dict of {filename: content}
```

## Mapping Reference

### Packages and Files

| Ecore | Proto |
|---|---|
| `EPackage` | One `.proto` file, `package` declaration |
| `eSubpackages` | Separate `.proto` file per sub-package |

### Classifiers

| Ecore | Proto |
|---|---|
| `EClass` | `message` |
| `EClass (abstract)` | `message` with `// Abstract base` comment |
| `EEnum` | `enum` with prefixed value names |
| `EDataType` | Resolved via `instanceClassName` to a scalar type |

### Structural Features

| Ecore | Proto |
|---|---|
| `EAttribute` | Scalar field (`string`, `int32`, `bool`, etc.) |
| `EReference` | Message-typed field |
| `EReference (containment=true)` | Message-typed field with `// containment` comment |
| `upperBound="-1"` | `repeated` field |
| `defaultValueLiteral` | Preserved as `// default: <value>` comment |
| `eOpposite` | Preserved as `// opposite: <path>` comment |

### Inheritance

Since proto3 does not support inheritance, super-type relationships are modeled as **composition**: the child message includes an embedded field of the parent message type.

```
// Ecore: Organization extends NamedElement
message Organization {
  // Inherited from NamedElement
  base.NamedElement named_element = 1;
  string website = 2;
  ...
}
```

### Type Mapping

| Ecore Type | Proto Type | Import Required |
|---|---|---|
| `EString` | `string` | — |
| `EInt` / `EInteger` | `int32` | — |
| `ELong` | `int64` | — |
| `EFloat` | `float` | — |
| `EDouble` | `double` | — |
| `EBoolean` | `bool` | — |
| `EByte` / `EShort` | `int32` | — |
| `EDate` | `google.protobuf.Timestamp` | `google/protobuf/timestamp.proto` |
| `EByteArray` | `bytes` | — |
| `EBigInteger` | `int64` | — |
| `EBigDecimal` | `string` | — |
| `EJavaObject` | `google.protobuf.Any` | `google/protobuf/any.proto` |

Java instance class names (`java.lang.String`, `int`, `double`, etc.) are also mapped through the same table.

## Example

Given two Ecore files where `domain.ecore` extends types from `base.ecore`:

```bash
python ecore_to_proto.py base.ecore domain.ecore -o output/ -v
```

**base.ecore** produces **base.proto**:

```protobuf
syntax = "proto3";

package base;

import "google/protobuf/timestamp.proto";

enum Status {
  STATUS_ACTIVE = 0;
  STATUS_INACTIVE = 1;
  STATUS_PENDING = 2;
}

message NamedElement {
  string id = 1;
  string name = 2;
  string description = 3;
  Status status = 4;  // default: ACTIVE
}

message AuditInfo {
  string created_by = 1;
  google.protobuf.Timestamp created_at = 2;
  string modified_by = 3;
  google.protobuf.Timestamp modified_at = 4;
}
```

**domain.ecore** produces **domain.proto**:

```protobuf
syntax = "proto3";

package domain;

import "base.proto";

enum Priority {
  PRIORITY_LOW = 0;
  PRIORITY_MEDIUM = 1;
  PRIORITY_HIGH = 2;
  PRIORITY_CRITICAL = 3;
}

message Organization {
  // Inherited from NamedElement
  base.NamedElement named_element = 1;
  string website = 2;
  int32 employee_count = 3;
  repeated Department departments = 4;  // containment
  base.AuditInfo audit_info = 5;  // containment
}

message Employee {
  // Inherited from NamedElement
  base.NamedElement named_element = 1;
  string email = 2;
  double salary = 3;
  bool is_active = 4;  // default: true
  repeated string tags = 5;
  Department department = 6;
}
```

## Limitations and Notes

- **Inheritance is composition, not flattening.** Fields from parent classes are not duplicated into children; instead, a single embedded parent message is included. This keeps proto files DRY but means accessors go through an extra level (e.g., `org.named_element.name`).
- **EAnnotations are not processed.** GenModel annotations, documentation annotations, and custom metadata are currently ignored.
- **EOperations are skipped.** Proto messages are data-only; Ecore operations have no proto equivalent.
- **Map detection is not automatic.** Ecore patterns that represent maps (e.g., `EStringToStringMapEntry`) are not converted to proto `map<K, V>` fields — they appear as regular messages.
- **No `oneof` generation.** Ecore union-like patterns are not detected. Consider post-processing if your model uses these.
- **Filename collisions.** If two packages produce the same snake_case filename, the second will overwrite the first. Use distinct package names to avoid this.

## License

MIT
