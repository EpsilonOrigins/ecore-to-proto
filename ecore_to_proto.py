#!/usr/bin/env python3
"""
Ecore to Protocol Buffers (.proto) Converter

Converts Eclipse EMF Ecore (.ecore) files into Protocol Buffer v3 (.proto) files.
Handles multiple interdependent Ecore files, cross-references, inheritance (via
composition), enums, and data type mapping.

Usage:
    python ecore_to_proto.py input1.ecore input2.ecore ... [-o output_dir]
    python ecore_to_proto.py ./models/ [-o output_dir]
"""

import argparse
import os
import sys
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path
from collections import OrderedDict


# ─── Ecore XMI Namespaces ────────────────────────────────────────────────────

ECORE_NS = "http://www.eclipse.org/emf/2002/Ecore"
XMI_NS = "http://www.omg.org/XMI"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

NS = {
    "ecore": ECORE_NS,
    "xmi": XMI_NS,
    "xsi": XSI_NS,
}


# ─── Data Model ──────────────────────────────────────────────────────────────

@dataclass
class EAnnotation:
    source: str                                    # annotation source URI/key
    details: dict = field(default_factory=dict)    # key -> value pairs

@dataclass
class EEnumLiteral:
    name: str
    value: int = 0

@dataclass
class EEnum:
    name: str
    literals: list = field(default_factory=list)  # list[EEnumLiteral]
    package_name: str = ""
    annotations: list = field(default_factory=list)  # list[EAnnotation]

@dataclass
class EAttribute:
    name: str
    e_type: str  # Ecore type string (e.g., "EString", "EInt")
    lower_bound: int = 0
    upper_bound: int = 1  # -1 means unbounded (repeated)
    default_value: Optional[str] = None
    source_hint: Optional[str] = None  # file/package hint from eType URI
    annotations: list = field(default_factory=list)  # list[EAnnotation]

@dataclass
class EReference:
    name: str
    e_type: str  # Referenced EClass name (possibly cross-package)
    containment: bool = False
    lower_bound: int = 0
    upper_bound: int = 1  # -1 means unbounded (repeated)
    opposite: Optional[str] = None
    resolved_package: Optional[str] = None  # Package where the type lives
    source_hint: Optional[str] = None  # file/package hint from eType URI
    annotations: list = field(default_factory=list)  # list[EAnnotation]

@dataclass
class EClass:
    name: str
    abstract: bool = False
    interface: bool = False
    super_types: list = field(default_factory=list)  # list[str]
    attributes: list = field(default_factory=list)   # list[EAttribute]
    references: list = field(default_factory=list)    # list[EReference]
    package_name: str = ""
    resolved_supers: list = field(default_factory=list)  # list[(pkg, class_name)]
    annotations: list = field(default_factory=list)  # list[EAnnotation]

@dataclass
class EDataType:
    name: str
    instance_class_name: Optional[str] = None

@dataclass
class EPackage:
    name: str
    ns_uri: str = ""
    ns_prefix: str = ""
    classes: list = field(default_factory=list)       # list[EClass]
    enums: list = field(default_factory=list)          # list[EEnum]
    data_types: list = field(default_factory=list)     # list[EDataType]
    sub_packages: list = field(default_factory=list)   # list[EPackage]
    source_file: str = ""
    annotations: list = field(default_factory=list)    # list[EAnnotation]


# ─── Ecore Type → Proto Type Mapping ─────────────────────────────────────────

ECORE_TO_PROTO_TYPE = {
    # Ecore built-in types
    "EString": "string",
    "EInt": "int32",
    "EInteger": "int32",
    "ELong": "int64",
    "EFloat": "float",
    "EDouble": "double",
    "EBoolean": "bool",
    "EByte": "int32",
    "EShort": "int32",
    "EChar": "string",
    "EDate": "google.protobuf.Timestamp",
    "EBigInteger": "int64",
    "EBigDecimal": "string",  # no native proto equivalent
    "EByteArray": "bytes",
    "EResource": "string",
    "EJavaObject": "google.protobuf.Any",
    "EJavaClass": "string",
    "EFeatureMapEntry": "google.protobuf.Any",
    "EMap": "google.protobuf.Struct",
    "EEList": "google.protobuf.ListValue",
    "ETreeIterator": "string",
    "EEnumerator": "int32",

    # Java primitive type names (from instanceClassName)
    "java.lang.String": "string",
    "java.lang.Integer": "int32",
    "java.lang.Long": "int64",
    "java.lang.Float": "float",
    "java.lang.Double": "double",
    "java.lang.Boolean": "bool",
    "java.lang.Byte": "int32",
    "java.lang.Short": "int32",
    "java.lang.Character": "string",
    "java.util.Date": "google.protobuf.Timestamp",
    "java.math.BigInteger": "int64",
    "java.math.BigDecimal": "string",
    "int": "int32",
    "long": "int64",
    "float": "float",
    "double": "double",
    "boolean": "bool",
    "byte": "int32",
    "short": "int32",
    "char": "string",
}

# Well-known proto types that require imports
WELL_KNOWN_TYPE_IMPORTS = {
    "google.protobuf.Timestamp": "google/protobuf/timestamp.proto",
    "google.protobuf.Any": "google/protobuf/any.proto",
    "google.protobuf.Struct": "google/protobuf/struct.proto",
    "google.protobuf.ListValue": "google/protobuf/struct.proto",
}


# ─── Parser ──────────────────────────────────────────────────────────────────

class EcoreParser:
    """Parses .ecore (XMI/XML) files into our data model."""

    def parse_file(self, filepath: str) -> list:
        """Parse an ecore file and return a list of EPackages."""
        tree = ET.parse(filepath)
        root = tree.getroot()

        # Handle different root element forms
        packages = []
        tag = self._strip_ns(root.tag)

        if tag == "EPackage":
            pkg = self._parse_package(root, filepath)
            packages.append(pkg)
        elif tag == "XMI" or tag == "Resource":
            for child in root:
                ctag = self._strip_ns(child.tag)
                if ctag == "EPackage":
                    packages.append(self._parse_package(child, filepath))
        else:
            # Try treating root as package anyway
            pkg = self._parse_package(root, filepath)
            if pkg.name:
                packages.append(pkg)

        return packages

    def _strip_ns(self, tag: str) -> str:
        if "}" in tag:
            return tag.split("}", 1)[1]
        return tag

    def _parse_package(self, elem, filepath: str) -> EPackage:
        pkg = EPackage(
            name=elem.get("name", ""),
            ns_uri=elem.get("nsURI", ""),
            ns_prefix=elem.get("nsPrefix", ""),
            source_file=filepath,
            annotations=self._parse_annotations(elem),
        )

        for child in elem:
            tag = self._strip_ns(child.tag)
            xsi_type = child.get(f"{{{XSI_NS}}}type", "")

            if tag == "eClassifiers" or tag == "EClassifiers":
                if "EClass" in xsi_type or xsi_type == "" and self._looks_like_class(child):
                    cls = self._parse_class(child, pkg.name)
                    pkg.classes.append(cls)
                elif "EEnum" in xsi_type or self._looks_like_enum(child):
                    enum = self._parse_enum(child, pkg.name)
                    pkg.enums.append(enum)
                elif "EDataType" in xsi_type or self._looks_like_datatype(child):
                    dt = self._parse_datatype(child)
                    pkg.data_types.append(dt)
                else:
                    # Guess based on content
                    if self._has_literals(child):
                        pkg.enums.append(self._parse_enum(child, pkg.name))
                    elif self._has_structural_features(child):
                        pkg.classes.append(self._parse_class(child, pkg.name))
                    else:
                        # Might be a data type or empty class
                        instance_cn = child.get("instanceClassName") or child.get("instanceTypeName")
                        if instance_cn:
                            pkg.data_types.append(self._parse_datatype(child))
                        else:
                            pkg.classes.append(self._parse_class(child, pkg.name))
            elif tag == "eSubpackages" or tag == "ESubpackages":
                sub = self._parse_package(child, filepath)
                pkg.sub_packages.append(sub)

        return pkg

    def _looks_like_class(self, elem) -> bool:
        return self._has_structural_features(elem) or elem.get("eSuperTypes") is not None or elem.get("abstract") is not None

    def _looks_like_enum(self, elem) -> bool:
        return self._has_literals(elem)

    def _looks_like_datatype(self, elem) -> bool:
        return elem.get("instanceClassName") is not None or elem.get("instanceTypeName") is not None

    def _has_literals(self, elem) -> bool:
        for child in elem:
            tag = self._strip_ns(child.tag)
            if "literal" in tag.lower() or "eLiterals" == tag:
                return True
        return False

    def _has_structural_features(self, elem) -> bool:
        for child in elem:
            tag = self._strip_ns(child.tag)
            if "eStructuralFeatures" == tag or "feature" in tag.lower():
                return True
        return False

    def _parse_class(self, elem, package_name: str) -> EClass:
        cls = EClass(
            name=elem.get("name", "UnknownClass"),
            abstract=elem.get("abstract", "false").lower() == "true",
            interface=elem.get("interface", "false").lower() == "true",
            package_name=package_name,
            annotations=self._parse_annotations(elem),
        )

        # Parse super types
        super_types_str = elem.get("eSuperTypes", "")
        if super_types_str:
            for st in super_types_str.split():
                cls.super_types.append(st.strip())

        # Parse structural features
        for child in elem:
            tag = self._strip_ns(child.tag)
            if tag == "eStructuralFeatures":
                xsi_type = child.get(f"{{{XSI_NS}}}type", "")
                if "EReference" in xsi_type:
                    cls.references.append(self._parse_reference(child))
                elif "EAttribute" in xsi_type:
                    cls.attributes.append(self._parse_attribute(child))
                else:
                    # Guess: if it has eType referencing a class, it's a reference
                    etype = child.get("eType", "")
                    if etype and ("#//" in etype or "ecore:" in etype.lower()):
                        if self._is_likely_reference(etype):
                            cls.references.append(self._parse_reference(child))
                        else:
                            cls.attributes.append(self._parse_attribute(child))
                    else:
                        cls.attributes.append(self._parse_attribute(child))

        return cls

    def _is_likely_reference(self, etype: str) -> bool:
        """Heuristic: if the type reference points to a class (not a built-in), it's a reference."""
        type_name = self._extract_type_name(etype)
        return type_name not in ECORE_TO_PROTO_TYPE

    def _parse_attribute(self, elem) -> EAttribute:
        etype_raw = elem.get("eType", "")
        type_name, source_hint = self._extract_type_info(etype_raw)

        return EAttribute(
            name=elem.get("name", "unknown"),
            e_type=type_name,
            lower_bound=int(elem.get("lowerBound", "0")),
            upper_bound=int(elem.get("upperBound", "1")),
            default_value=elem.get("defaultValueLiteral"),
            source_hint=source_hint,
            annotations=self._parse_annotations(elem),
        )

    def _parse_reference(self, elem) -> EReference:
        etype_raw = elem.get("eType", "")
        type_name, source_hint = self._extract_type_info(etype_raw)

        return EReference(
            name=elem.get("name", "unknown"),
            e_type=type_name,
            containment=elem.get("containment", "false").lower() == "true",
            lower_bound=int(elem.get("lowerBound", "0")),
            upper_bound=int(elem.get("upperBound", "1")),
            opposite=elem.get("eOpposite"),
            source_hint=source_hint,
            annotations=self._parse_annotations(elem),
        )

    def _parse_annotations(self, elem) -> list:
        """Parse all eAnnotations children of an element.

        Ecore annotations look like:
          <eAnnotations source="http://www.eclipse.org/emf/2002/GenModel">
            <details key="documentation" value="This is the description"/>
          </eAnnotations>
          <eAnnotations source="http://example.com/ui">
            <details key="label" value="Full Name"/>
            <details key="tooltip" value="Enter your full legal name"/>
            <details key="readonly" value="true"/>
          </eAnnotations>
        """
        annotations = []
        for child in elem:
            tag = self._strip_ns(child.tag)
            if tag == "eAnnotations":
                source = child.get("source", "")
                details = {}
                for detail in child:
                    dtag = self._strip_ns(detail.tag)
                    if dtag == "details":
                        key = detail.get("key", "")
                        value = detail.get("value", "")
                        if key:
                            details[key] = value
                if details:
                    annotations.append(EAnnotation(source=source, details=details))
        return annotations

    def _parse_enum(self, elem, package_name: str) -> EEnum:
        enum = EEnum(
            name=elem.get("name", "UnknownEnum"),
            package_name=package_name,
            annotations=self._parse_annotations(elem),
        )
        for child in elem:
            tag = self._strip_ns(child.tag)
            if tag == "eLiterals":
                literal = EEnumLiteral(
                    name=child.get("name", "UNKNOWN"),
                    value=int(child.get("value", "0")),
                )
                enum.literals.append(literal)
        return enum

    def _parse_datatype(self, elem) -> EDataType:
        return EDataType(
            name=elem.get("name", ""),
            instance_class_name=elem.get("instanceClassName") or elem.get("instanceTypeName"),
        )

    def _extract_type_info(self, etype_raw: str) -> tuple:
        """Extract (type_name, source_hint) from eType attribute values.

        source_hint is the file/package prefix that tells us where the type lives:
        - '#//MyClass'                      → ('MyClass', None)         — same file
        - 'assetcommon.ecore#//Logging'     → ('Logging', 'assetcommon')
        - 'ecore:EDataType http://...#//EString' → ('EString', None)   — built-in
        - '#//packageName/ClassName'        → ('ClassName', None)       — same file subpackage
        """
        if not etype_raw:
            return ("EString", None)

        source_hint = None

        if "#//" in etype_raw:
            # Split on '#//' — left side has the file reference, right side has the type path
            left, right = etype_raw.rsplit("#//", 1)
            type_name = right.split("/")[-1].strip()

            # Extract source hint from left side
            # Could be: '', 'assetcommon.ecore', 'ecore:EDataType http://.../Ecore',
            #           '../other/path/model.ecore', 'platform:/resource/proj/model.ecore'
            left = left.strip()
            if left and "eclipse.org/emf" not in left and "www.w3.org" not in left:
                # Strip any 'ecore:EClass' or 'ecore:EDataType' prefix
                if " " in left:
                    left = left.split()[-1]
                # Get the filename stem (without path and extension)
                # 'assetcommon.ecore' -> 'assetcommon'
                # '../models/assetcommon.ecore' -> 'assetcommon'
                # 'platform:/resource/proj/assetcommon.ecore' -> 'assetcommon'
                basename = left.rsplit("/", 1)[-1]  # get filename part
                if "." in basename:
                    source_hint = basename.rsplit(".", 1)[0]  # strip extension
                else:
                    source_hint = basename
                if source_hint:
                    source_hint = source_hint.strip()

            return (type_name, source_hint)

        # Plain type name (no '#//')
        parts = etype_raw.split()
        return (parts[-1].strip(), None)

    def _extract_type_name(self, etype_raw: str) -> str:
        """Convenience wrapper returning just the type name."""
        type_name, _ = self._extract_type_info(etype_raw)
        return type_name


# ─── Cross-Reference Resolver ────────────────────────────────────────────────

class ReferenceResolver:
    """Resolves cross-references between packages from multiple ecore files.

    Uses source hints from eType URIs (e.g., 'assetcommon.ecore#//Logging')
    to disambiguate when multiple packages define a type with the same name.

    Resolution priority:
      1. Qualified name match (e.g., 'assetcommon.Logging')
      2. Source hint from eType URI matches a package/file
      3. Same-package match (type defined in the current package)
      4. First registered match (fallback)
    """

    def __init__(self, packages: list):
        self.packages = packages  # list[EPackage]

        # name -> [pkg_name, ...] — all packages that define this class/enum
        self.class_index: dict[str, list[str]] = {}
        self.enum_index: dict[str, list[str]] = {}
        self.datatype_index: dict[str, str] = {}  # datatype_name -> instance_class_name

        # Maps file stems and package names so we can resolve source hints
        # e.g., 'assetcommon' -> 'assetcommon' (trivial), but also handles
        # cases where the filename differs from the package name
        self.file_stem_to_package: dict[str, str] = {}

        self._build_indices()

    def _build_indices(self):
        for pkg in self.packages:
            self._index_package(pkg)

    def _index_package(self, pkg: EPackage):
        # Map file stem -> package name
        if pkg.source_file:
            stem = Path(pkg.source_file).stem  # 'assetcommon.ecore' -> 'assetcommon'
            self.file_stem_to_package[stem] = pkg.name
        # Also map package name to itself for direct matches
        self.file_stem_to_package[pkg.name] = pkg.name

        for cls in pkg.classes:
            self.class_index.setdefault(cls.name, [])
            if pkg.name not in self.class_index[cls.name]:
                self.class_index[cls.name].append(pkg.name)
        for enum in pkg.enums:
            self.enum_index.setdefault(enum.name, [])
            if pkg.name not in self.enum_index[enum.name]:
                self.enum_index[enum.name].append(pkg.name)
        for dt in pkg.data_types:
            if dt.instance_class_name:
                self.datatype_index[dt.name] = dt.instance_class_name
        for sub in pkg.sub_packages:
            self._index_package(sub)

    def resolve_type(self, type_name: str, source_hint: Optional[str],
                     current_pkg_name: str, index: dict) -> Optional[str]:
        """Resolve a type name to the correct package using disambiguation.

        Args:
            type_name: The unqualified type name (e.g., 'Logging')
            source_hint: File stem hint from the eType URI (e.g., 'assetcommon'), or None
            current_pkg_name: Name of the package where the reference lives
            index: The class_index or enum_index to search

        Returns:
            The resolved package name, or None if not found.
        """
        candidates = index.get(type_name, [])

        if not candidates:
            return None

        # Only one match — no ambiguity
        if len(candidates) == 1:
            return candidates[0]

        # Multiple candidates — disambiguate

        # 1. Source hint match: URI said 'assetcommon.ecore#//Logging'
        if source_hint:
            # The hint might be a file stem that maps to a package name
            hinted_pkg = self.file_stem_to_package.get(source_hint)
            if hinted_pkg and hinted_pkg in candidates:
                return hinted_pkg
            # Or the hint might directly be a package name
            if source_hint in candidates:
                return source_hint

        # 2. Same-package match
        if current_pkg_name in candidates:
            return current_pkg_name

        # 3. Fallback to first registered
        return candidates[0]

    def resolve(self):
        """Resolve all cross-references in all packages."""
        for pkg in self.packages:
            self._resolve_package(pkg)

    def _resolve_package(self, pkg: EPackage):
        for cls in pkg.classes:
            # Resolve super types
            for st in cls.super_types:
                type_name, source_hint = self._extract_type_info_from_uri(st)
                target_pkg = self.resolve_type(
                    type_name, source_hint, pkg.name, self.class_index
                )
                cls.resolved_supers.append((target_pkg or pkg.name, type_name))

            # Resolve references
            for ref in cls.references:
                resolved = self.resolve_type(
                    ref.e_type, ref.source_hint, pkg.name, self.class_index
                )
                if resolved:
                    ref.resolved_package = resolved
                else:
                    # Try enum index
                    resolved = self.resolve_type(
                        ref.e_type, ref.source_hint, pkg.name, self.enum_index
                    )
                    ref.resolved_package = resolved or pkg.name

        for sub in pkg.sub_packages:
            self._resolve_package(sub)

    def _extract_type_info_from_uri(self, uri: str) -> tuple:
        """Extract (type_name, source_hint) from a raw eSuperTypes URI.

        Examples:
            '#//NamedElement'                  → ('NamedElement', None)
            'assetcommon.ecore#//Logging'      → ('Logging', 'assetcommon')
            'base.ecore#//NamedElement'        → ('NamedElement', 'base')
        """
        source_hint = None
        type_name = uri

        if "#//" in uri:
            left, right = uri.rsplit("#//", 1)
            type_name = right.split("/")[-1].strip()

            left = left.strip()
            if left:
                # Get file stem from paths like '../models/assetcommon.ecore'
                basename = left.rsplit("/", 1)[-1]
                if "." in basename:
                    source_hint = basename.rsplit(".", 1)[0]
                else:
                    source_hint = basename
        else:
            type_name = uri.split("/")[-1]

        return (type_name, source_hint)


# ─── Annotation Collector & UI Options Generator ─────────────────────────────

# Starting field number for custom extensions (protobuf convention: 50000+)
_EXTENSION_FIELD_START = 50000

def _infer_proto_type(values: set) -> str:
    """Infer the best proto type from a set of observed annotation values."""
    if not values:
        return "string"
    # Check if all values are boolean
    if all(v.lower() in ("true", "false") for v in values):
        return "bool"
    # Check if all values are integers
    if all(re.fullmatch(r'-?\d+', v) for v in values):
        return "int32"
    # Check if all values are floats
    if all(re.fullmatch(r'-?\d+\.?\d*', v) for v in values):
        return "double"
    return "string"

def _shorten_source(source: str) -> str:
    """Convert an annotation source URI to a short readable name.

    Examples:
        'http://www.eclipse.org/emf/2002/GenModel' → 'genmodel'
        'http://example.com/ui'                     → 'ui'
        'http://example.com/constraints/validation' → 'validation'
        'myCustomSource'                            → 'mycustomsource'
    """
    if not source:
        return "unknown"
    # Take the last meaningful path segment
    # Strip trailing slashes, split by '/', take last non-empty part
    cleaned = source.rstrip("/")
    if "/" in cleaned:
        last = cleaned.rsplit("/", 1)[-1]
    else:
        last = cleaned
    # Clean up
    last = re.sub(r'[^a-zA-Z0-9_]', '_', last).lower()
    last = re.sub(r'_+', '_', last).strip('_')
    return last or "unknown"


@dataclass
class AnnotationOptionDef:
    """A single option field inside extend google.protobuf.FieldOptions."""
    key: str              # original annotation detail key
    field_name: str       # snake_case proto field name
    proto_type: str       # inferred proto type (string, bool, int32, double)
    field_number: int     # assigned extension field number
    source_short: str     # shortened annotation source
    source_full: str      # full annotation source URI


class AnnotationCollector:
    """Scans all packages and builds a registry of unique annotation detail keys.

    Each unique (source, detail_key) pair becomes an extension field in the
    generated ui_options.proto file.
    """

    def __init__(self):
        # (source_short, key) -> AnnotationOptionDef
        self.options: OrderedDict[tuple, AnnotationOptionDef] = OrderedDict()
        # (source_short, key) -> set of observed values (for type inference)
        self._value_samples: dict[tuple, set] = {}
        self._next_field_number = _EXTENSION_FIELD_START

    def scan_packages(self, packages: list):
        """Walk all packages, classes, attributes, references to collect annotations."""
        for pkg in packages:
            self._scan_package(pkg)
        # After scanning, finalize inferred types and resolve field name collisions
        self._finalize()

    def _scan_package(self, pkg: EPackage):
        for ann in pkg.annotations:
            self._register_annotation(ann)
        for cls in pkg.classes:
            for ann in cls.annotations:
                self._register_annotation(ann)
            for attr in cls.attributes:
                for ann in attr.annotations:
                    self._register_annotation(ann)
            for ref in cls.references:
                for ann in ref.annotations:
                    self._register_annotation(ann)
        for enum in pkg.enums:
            for ann in enum.annotations:
                self._register_annotation(ann)
        for sub in pkg.sub_packages:
            self._scan_package(sub)

    def _register_annotation(self, ann: EAnnotation):
        source_short = _shorten_source(ann.source)
        for key, value in ann.details.items():
            lookup = (source_short, key)
            if lookup not in self.options:
                self.options[lookup] = AnnotationOptionDef(
                    key=key,
                    field_name="",  # assigned during _finalize
                    proto_type="string",  # placeholder, finalized later
                    field_number=self._next_field_number,
                    source_short=source_short,
                    source_full=ann.source,
                )
                self._value_samples[lookup] = set()
                self._next_field_number += 1
            self._value_samples[lookup].add(value)

    def _finalize(self):
        """Finalize inferred types and assign field names, prefixing only on collision."""
        # Detect which raw keys appear under multiple sources
        key_to_sources: dict[str, set] = {}
        for (source_short, key) in self.options:
            key_to_sources.setdefault(key, set()).add(source_short)

        for lookup, opt in self.options.items():
            source_short, key = lookup
            # Infer proto type
            opt.proto_type = _infer_proto_type(self._value_samples.get(lookup, set()))
            # Only prefix with source when the same key exists under multiple sources
            needs_prefix = len(key_to_sources.get(key, set())) > 1
            opt.field_name = self._to_option_field_name(
                source_short if needs_prefix else "", key
            )

    @staticmethod
    def _to_option_field_name(source_short: str, key: str) -> str:
        """Build a snake_case field name, only prefixing with source on collision.

        e.g., ('', 'displayName')          → 'display_name'
              ('', 'documentation')        → 'documentation'
              ('genmodel', 'documentation') → 'genmodel_documentation'  (collision)
        """
        raw = f"{source_short}_{key}" if source_short else key
        s = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', raw)
        s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', s)
        s = s.lower()
        s = re.sub(r'[^a-z0-9_]', '_', s)
        s = re.sub(r'_+', '_', s).strip('_')
        return s

    def has_annotations(self) -> bool:
        return len(self.options) > 0

    def get_option_for(self, ann: EAnnotation, key: str) -> Optional[AnnotationOptionDef]:
        """Look up the option definition for a given annotation detail key."""
        source_short = _shorten_source(ann.source)
        return self.options.get((source_short, key))

    def generate_ui_options_proto(self, options_package: str = "ui",
                                    proto_package_prefix: str = "") -> str:
        """Generate the options proto file content."""
        lines = []
        lines.append('syntax = "proto3";')
        lines.append("")

        pkg_name = options_package
        if proto_package_prefix:
            pkg_name = f"{proto_package_prefix}.{options_package}"
        lines.append(f"package {pkg_name};")
        lines.append("")
        lines.append('import "google/protobuf/descriptor.proto";')
        lines.append("")

        # Group options by source for readability
        by_source: OrderedDict[str, list] = OrderedDict()
        for (source_short, _), opt in self.options.items():
            by_source.setdefault(source_short, []).append(opt)

        lines.append("extend google.protobuf.FieldOptions {")
        first_group = True
        for source_short, opts in by_source.items():
            if not first_group:
                lines.append("")
            # Comment with source group header and full URI
            full_uri = opts[0].source_full
            lines.append(f"  // Source: {full_uri}")
            for opt in opts:
                lines.append(
                    f"  optional {opt.proto_type} {opt.field_name} = {opt.field_number};"
                )
            first_group = False
        lines.append("}")
        lines.append("")

        return "\n".join(lines)


# ─── Proto Generator ─────────────────────────────────────────────────────────

class ProtoGenerator:
    """Generates .proto file content from parsed Ecore packages."""

    def __init__(self, all_packages: list, resolver: ReferenceResolver,
                 annotation_collector: Optional[AnnotationCollector] = None,
                 options_package: str = "ui",
                 java_package_prefix: str = "", go_package_prefix: str = "",
                 proto_package_prefix: str = ""):
        self.all_packages = all_packages
        self.resolver = resolver
        self.annotations = annotation_collector
        self.options_package = options_package
        self.options_filename = f"{options_package}_options.proto"
        self.java_package_prefix = java_package_prefix
        self.go_package_prefix = go_package_prefix
        self.proto_package_prefix = proto_package_prefix

    def generate_all(self) -> dict:
        """Generate proto content for all packages. Returns {filename: content}."""
        result = OrderedDict()

        # Generate options proto if annotations were found
        if self.annotations and self.annotations.has_annotations():
            result[self.options_filename] = self.annotations.generate_ui_options_proto(
                options_package=self.options_package,
                proto_package_prefix=self.proto_package_prefix,
            )

        for pkg in self.all_packages:
            files = self._generate_package(pkg)
            result.update(files)
        return result

    def _generate_package(self, pkg: EPackage) -> dict:
        result = OrderedDict()
        filename = self._package_to_filename(pkg.name)
        content = self._generate_proto_file(pkg)
        result[filename] = content

        for sub in pkg.sub_packages:
            sub_files = self._generate_package(sub)
            result.update(sub_files)

        return result

    def _package_to_filename(self, name: str) -> str:
        # Convert CamelCase/UPPER_CASE to snake_case
        if name == name.upper() or '_' in name:
            s = name.lower()
        else:
            s = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', name)
            s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', s)
            s = s.lower()
        s = re.sub(r'[^a-z0-9_]', '_', s)
        s = re.sub(r'_+', '_', s).strip('_')
        return f"{s}.proto"

    def _generate_proto_file(self, pkg: EPackage) -> str:
        lines = []
        imports_needed = set()
        proto_pkg_name = self._to_proto_package(pkg.name)

        # Collect imports
        for cls in pkg.classes:
            cls_imports = self._collect_imports(cls, pkg)
            imports_needed.update(cls_imports)

        # Header
        lines.append('syntax = "proto3";')
        lines.append("")
        lines.append(f"package {proto_pkg_name};")
        lines.append("")

        # Options
        if self.java_package_prefix:
            lines.append(f'option java_package = "{self.java_package_prefix}.{pkg.name}";')
        if self.go_package_prefix:
            go_pkg = f"{self.go_package_prefix}/{pkg.name.lower()}"
            lines.append(f'option go_package = "{go_pkg}";')
        if self.java_package_prefix or self.go_package_prefix:
            lines.append("")

        # Imports
        sorted_imports = sorted(imports_needed)
        if sorted_imports:
            for imp in sorted_imports:
                lines.append(f'import "{imp}";')
            lines.append("")

        # Enums (file-level)
        for enum in pkg.enums:
            lines.extend(self._generate_enum(enum, indent=0))
            lines.append("")

        # Messages
        for cls in pkg.classes:
            lines.extend(self._generate_message(cls, pkg))
            lines.append("")

        # Remove trailing blank lines
        while lines and lines[-1] == "":
            lines.pop()
        lines.append("")  # single trailing newline

        return "\n".join(lines)

    def _collect_imports(self, cls: EClass, current_pkg: EPackage) -> set:
        imports = set()

        # Check attributes for well-known types and annotations
        for attr in cls.attributes:
            proto_type = self._resolve_attribute_type(attr, current_pkg)
            if proto_type in WELL_KNOWN_TYPE_IMPORTS:
                imports.add(WELL_KNOWN_TYPE_IMPORTS[proto_type])
            if attr.annotations and self.annotations and self.annotations.has_annotations():
                imports.add(self.options_filename)

        # Check references for cross-package imports and annotations
        for ref in cls.references:
            if ref.resolved_package and ref.resolved_package != current_pkg.name:
                imports.add(self._package_to_filename(ref.resolved_package))
            if ref.annotations and self.annotations and self.annotations.has_annotations():
                imports.add(self.options_filename)

        # Check supers for cross-package imports
        for (super_pkg, super_name) in cls.resolved_supers:
            if super_pkg != current_pkg.name:
                imports.add(self._package_to_filename(super_pkg))

        return imports

    def _generate_enum(self, enum: EEnum, indent: int = 0) -> list:
        prefix = "  " * indent
        lines = []
        enum_name = self._to_proto_name(enum.name)
        lines.append(f"{prefix}enum {enum_name} {{")

        # Proto3 requires first enum value to be 0
        has_zero = any(lit.value == 0 for lit in enum.literals)
        if not has_zero:
            unspecified_name = self._to_enum_value_name(enum.name, "UNSPECIFIED")
            lines.append(f"{prefix}  {unspecified_name} = 0;")

        for lit in enum.literals:
            value_name = self._to_enum_value_name(enum.name, lit.name)
            lines.append(f"{prefix}  {value_name} = {lit.value};")

        lines.append(f"{prefix}}}")
        return lines

    def _generate_message(self, cls: EClass, current_pkg: EPackage) -> list:
        lines = []
        msg_name = self._to_proto_name(cls.name)

        # Add comment for abstract classes
        if cls.abstract:
            lines.append(f"// Abstract base: {cls.name}")
        if cls.interface:
            lines.append(f"// Interface: {cls.name}")

        # Class-level annotation comments
        if cls.annotations:
            for ann in cls.annotations:
                for key, value in ann.details.items():
                    lines.append(f"// @{_shorten_source(ann.source)}.{key}: {value}")

        lines.append(f"message {msg_name} {{")

        field_number = 1

        # Inheritance: include fields from super types as embedded messages
        for (super_pkg, super_name) in cls.resolved_supers:
            proto_super_name = self._to_proto_name(super_name)
            if super_pkg != current_pkg.name:
                proto_super_name = f"{self._to_proto_package(super_pkg)}.{proto_super_name}"
            field_name = self._to_field_name(super_name)
            lines.append(f"  // Inherited from {super_name}")
            lines.append(f"  {proto_super_name} {field_name} = {field_number};")
            field_number += 1

        # Attributes
        for attr in cls.attributes:
            proto_type = self._resolve_attribute_type(attr, current_pkg)
            field_name = self._to_field_name(attr.name)
            repeated = "repeated " if attr.upper_bound == -1 else ""

            option_parts = self._collect_field_options(attr.annotations)
            comment = ""
            if attr.default_value is not None:
                comment = f"  // default: {attr.default_value}"

            lines.extend(self._format_field_line(
                f"{repeated}{proto_type}", field_name, field_number,
                option_parts, comment
            ))
            field_number += 1

        # References
        for ref in cls.references:
            proto_type = self._resolve_reference_type(ref, current_pkg)
            field_name = self._to_field_name(ref.name)
            repeated = "repeated " if ref.upper_bound == -1 else ""

            option_parts = self._collect_field_options(ref.annotations)
            comment_parts = []
            if ref.containment:
                comment_parts.append("containment")
            if ref.opposite:
                comment_parts.append(f"opposite: {ref.opposite}")
            comment = f"  // {', '.join(comment_parts)}" if comment_parts else ""

            lines.extend(self._format_field_line(
                f"{repeated}{proto_type}", field_name, field_number,
                option_parts, comment
            ))
            field_number += 1

        lines.append("}")
        return lines

    def _collect_field_options(self, annotations: list) -> list:
        """Collect annotation options as a list of formatted option strings.

        Returns list like:
            ['(ui.label) = "Email Address"', '(ui.readonly) = true']
        """
        if not annotations or not self.annotations:
            return []

        parts = []
        for ann in annotations:
            for key, value in ann.details.items():
                opt_def = self.annotations.get_option_for(ann, key)
                if opt_def:
                    formatted_value = self._format_option_value(value, opt_def.proto_type)
                    parts.append(f"({self.options_package}.{opt_def.field_name}) = {formatted_value}")
        return parts

    def _format_field_line(self, type_str: str, field_name: str,
                           field_number: int, option_parts: list,
                           comment: str) -> list:
        """Format a field declaration with options.

        Single option  → inline:   string name = 1 [(ui.label) = "Name"];
        Multiple opts  → block:    string email = 4 [
                                     (ui.label) = "Email",
                                     (ui.required) = true
                                   ];
        No options     → plain:    string name = 1;
        """
        if not option_parts:
            return [f"  {type_str} {field_name} = {field_number};{comment}"]

        if len(option_parts) == 1:
            return [f"  {type_str} {field_name} = {field_number} [{option_parts[0]}];{comment}"]

        # Multi-line block format
        lines = []
        lines.append(f"  {type_str} {field_name} = {field_number} [")
        for i, opt in enumerate(option_parts):
            comma = "," if i < len(option_parts) - 1 else ""
            lines.append(f"    {opt}{comma}")
        lines.append(f"  ];{comment}")
        return lines

    def _resolve_attribute_type(self, attr: EAttribute, current_pkg: EPackage) -> str:
        type_name = attr.e_type

        # Direct mapping (built-in Ecore types)
        if type_name in ECORE_TO_PROTO_TYPE:
            return ECORE_TO_PROTO_TYPE[type_name]

        # Check if it's a custom data type in the current package
        for dt in current_pkg.data_types:
            if dt.name == type_name and dt.instance_class_name:
                if dt.instance_class_name in ECORE_TO_PROTO_TYPE:
                    return ECORE_TO_PROTO_TYPE[dt.instance_class_name]

        # Check if it's an enum in the current package
        for enum in current_pkg.enums:
            if enum.name == type_name:
                return self._to_proto_name(type_name)

        # Check global enum index (with disambiguation)
        enum_pkg = self.resolver.resolve_type(
            type_name, attr.source_hint, current_pkg.name, self.resolver.enum_index
        )
        if enum_pkg:
            if enum_pkg == current_pkg.name:
                return self._to_proto_name(type_name)
            else:
                return f"{self._to_proto_package(enum_pkg)}.{self._to_proto_name(type_name)}"

        # Check global datatype index
        if type_name in self.resolver.datatype_index:
            java_type = self.resolver.datatype_index[type_name]
            if java_type in ECORE_TO_PROTO_TYPE:
                return ECORE_TO_PROTO_TYPE[java_type]

        # Fallback
        return "string"

    def _resolve_reference_type(self, ref: EReference, current_pkg: EPackage) -> str:
        type_name = ref.e_type

        # Check if it's in the same package
        for cls in current_pkg.classes:
            if cls.name == type_name:
                return self._to_proto_name(type_name)

        # Check cross-package
        if ref.resolved_package and ref.resolved_package != current_pkg.name:
            return f"{self._to_proto_package(ref.resolved_package)}.{self._to_proto_name(type_name)}"

        return self._to_proto_name(type_name)

    # ── Naming & Formatting Helpers ──────────────────────────────────────

    @staticmethod
    def _format_option_value(value: str, proto_type: str) -> str:
        """Format an annotation value for proto field option syntax."""
        if proto_type == "bool":
            return value.lower()
        elif proto_type in ("int32", "int64"):
            return value
        elif proto_type in ("float", "double"):
            return value
        else:
            # String: escape quotes and wrap
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'

    def _to_proto_package(self, name: str) -> str:
        """Convert package name to proto package style."""
        pkg = name.lower().replace("-", "_").replace(".", "_")
        if self.proto_package_prefix:
            return f"{self.proto_package_prefix}.{pkg}"
        return pkg

    def _to_proto_name(self, name: str) -> str:
        """Ensure PascalCase for message/enum names."""
        if not name:
            return "Unknown"
        return name[0].upper() + name[1:]

    def _to_field_name(self, name: str) -> str:
        """Convert to snake_case for proto field names.
        Handles camelCase, PascalCase, UPPER_SNAKE_CASE, and ALL_CAPS correctly.
        """
        # If already snake_case or UPPER_SNAKE_CASE, just lowercase it
        if name == name.upper() or '_' in name:
            s = name.lower()
        else:
            # CamelCase / PascalCase: insert underscores at boundaries
            # Between lowercase/digit and uppercase: "employeeCount" -> "employee_Count"
            s = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', name)
            # Between uppercase run and uppercase+lowercase: "XMLParser" -> "XML_Parser"
            s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', s)
            s = s.lower()
        # Clean up non-alphanumeric characters
        s = re.sub(r'[^a-z0-9_]', '_', s)
        s = re.sub(r'_+', '_', s).strip('_')
        # Ensure it doesn't start with a digit
        if s and s[0].isdigit():
            s = f"field_{s}"
        return s or "unknown"

    def _to_enum_value_name(self, enum_name: str, literal_name: str) -> str:
        """Convert to UPPER_SNAKE_CASE with enum prefix."""
        prefix = self._camel_to_upper_snake(enum_name)
        value = self._camel_to_upper_snake(literal_name)
        # Avoid double-prefix if literal already starts with enum name
        if value.startswith(prefix):
            return value
        return f"{prefix}_{value}"

    @staticmethod
    def _camel_to_upper_snake(name: str) -> str:
        """Convert CamelCase or ALLCAPS to UPPER_SNAKE_CASE properly."""
        # If already UPPER_SNAKE_CASE, just clean it
        if name == name.upper():
            return re.sub(r'[^A-Z0-9_]', '_', name).strip('_')
        # Insert underscore between: lowercase->uppercase, uppercase->uppercase+lowercase
        s = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', name)
        s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', s)
        s = re.sub(r'[^A-Za-z0-9_]', '_', s)
        return s.upper().strip('_')


# ─── Flatten Subpackages ─────────────────────────────────────────────────────

def flatten_packages(packages: list) -> list:
    """Recursively flatten sub-packages into a flat list."""
    result = []
    for pkg in packages:
        result.append(pkg)
        if pkg.sub_packages:
            result.extend(flatten_packages(pkg.sub_packages))
    return result


# ─── Main ─────────────────────────────────────────────────────────────────────

def find_ecore_files(paths: list) -> list:
    """Given a list of file/directory paths, find all .ecore files."""
    files = []
    for p in paths:
        path = Path(p)
        if path.is_file() and path.suffix == ".ecore":
            files.append(str(path))
        elif path.is_dir():
            for f in sorted(path.rglob("*.ecore")):
                files.append(str(f))
        else:
            print(f"Warning: skipping '{p}' (not a .ecore file or directory)", file=sys.stderr)
    return files


def convert(ecore_files: list, output_dir: str = ".",
            options_package: str = "ui",
            java_package: str = "", go_package: str = "",
            proto_package: str = "", verbose: bool = False) -> dict:
    """
    Convert a list of ecore files to proto files.

    Returns a dict of {filename: proto_content}.
    """
    parser = EcoreParser()
    all_packages = []

    for filepath in ecore_files:
        if verbose:
            print(f"Parsing: {filepath}", file=sys.stderr)
        try:
            packages = parser.parse_file(filepath)
            all_packages.extend(packages)
        except ET.ParseError as e:
            print(f"Error parsing {filepath}: {e}", file=sys.stderr)
            continue

    if not all_packages:
        print("No packages found in the provided ecore files.", file=sys.stderr)
        return {}

    # Flatten sub-packages
    flat_packages = flatten_packages(all_packages)

    if verbose:
        print(f"\nFound {len(flat_packages)} package(s):", file=sys.stderr)
        for pkg in flat_packages:
            print(f"  - {pkg.name}: {len(pkg.classes)} classes, "
                  f"{len(pkg.enums)} enums, {len(pkg.data_types)} data types",
                  file=sys.stderr)

    # Resolve cross-references
    resolver = ReferenceResolver(flat_packages)
    resolver.resolve()

    # Collect annotations across all packages
    annotation_collector = AnnotationCollector()
    annotation_collector.scan_packages(flat_packages)

    if verbose and annotation_collector.has_annotations():
        print(f"\nFound {len(annotation_collector.options)} unique annotation option(s):",
              file=sys.stderr)
        for (src, key), opt in annotation_collector.options.items():
            print(f"  - [{src}] {key} → {opt.proto_type} (field #{opt.field_number})",
                  file=sys.stderr)

    # Generate proto files
    generator = ProtoGenerator(
        flat_packages, resolver,
        annotation_collector=annotation_collector,
        options_package=options_package,
        java_package_prefix=java_package,
        go_package_prefix=go_package,
        proto_package_prefix=proto_package,
    )
    proto_files = generator.generate_all()

    # Write output
    os.makedirs(output_dir, exist_ok=True)
    for filename, content in proto_files.items():
        outpath = os.path.join(output_dir, filename)
        with open(outpath, "w") as f:
            f.write(content)
        if verbose:
            print(f"Generated: {outpath}", file=sys.stderr)

    return proto_files


def main():
    parser = argparse.ArgumentParser(
        description="Convert Eclipse Ecore (.ecore) files to Protocol Buffer (.proto) files.",
        epilog="Examples:\n"
               "  %(prog)s model.ecore\n"
               "  %(prog)s models/ -o proto_out/ -v\n"
               "  %(prog)s base.ecore domain.ecore -o output/ --java-package com.example\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("inputs", nargs="+",
                        help="Ecore files or directories containing ecore files")
    parser.add_argument("-o", "--output-dir", default=".",
                        help="Output directory for .proto files (default: current dir)")
    parser.add_argument("--java-package", default="",
                        help="Java package prefix for generated protos")
    parser.add_argument("--go-package", default="",
                        help="Go package prefix for generated protos")
    parser.add_argument("--proto-package", default="",
                        help="Proto package prefix")
    parser.add_argument("--options-package", default="ui",
                        help="Package name for annotation field options proto "
                             "(default: ui → ui_options.proto)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Print verbose output")

    args = parser.parse_args()

    ecore_files = find_ecore_files(args.inputs)
    if not ecore_files:
        print("No .ecore files found.", file=sys.stderr)
        sys.exit(1)

    if args.verbose:
        print(f"Found {len(ecore_files)} ecore file(s):", file=sys.stderr)
        for f in ecore_files:
            print(f"  {f}", file=sys.stderr)

    proto_files = convert(
        ecore_files,
        output_dir=args.output_dir,
        options_package=args.options_package,
        java_package=args.java_package,
        go_package=args.go_package,
        proto_package=args.proto_package,
        verbose=args.verbose,
    )

    if proto_files:
        print(f"\nSuccessfully generated {len(proto_files)} proto file(s) in '{args.output_dir}':")
        for fname in proto_files:
            print(f"  {fname}")
    else:
        print("No proto files were generated.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
