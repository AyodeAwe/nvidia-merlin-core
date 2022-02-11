# Copyright (c) 2021, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import os
import pathlib
from typing import Union

import fsspec
import numpy

os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"
from google.protobuf import json_format  # noqa: E402
from google.protobuf.any_pb2 import Any as AnyPb2  # noqa: E402
from google.protobuf.struct_pb2 import Struct  # noqa: E402

from ..schema import ColumnSchema  # noqa: E402
from ..schema import Schema as MerlinSchema  # noqa: E402
from ..tags import Tags  # noqa: E402
from . import proto_utils  # noqa: E402
from . import schema_bp  # noqa: E402
from .schema_bp import ValueCount  # noqa: E402
from .schema_bp import Feature, FeatureType, FixedShape, FloatDomain, IntDomain  # noqa: E402
from .schema_bp import Schema as ProtoSchema  # noqa: E402

DOMAIN_ATTRS = {FeatureType.INT: "int_domain", FeatureType.FLOAT: "float_domain"}
FEATURE_TYPES = {
    "int": FeatureType.INT,
    "uint": FeatureType.INT,
    "float": FeatureType.FLOAT,
}


class TensorflowMetadata:
    def __init__(self, schema: ProtoSchema = None):
        self.proto_schema = schema

    @classmethod
    def from_json(cls, json: Union[str, bytes]) -> "TensorflowMetadata":
        schema = ProtoSchema().from_json(json)
        return TensorflowMetadata(schema)

    @classmethod
    def from_json_file(cls, path: str) -> "TensorflowMetadata":
        return cls.from_json(_read_file(path))

    @classmethod
    def from_proto_text(cls, path_or_proto_text: str) -> "TensorflowMetadata":
        from tensorflow_metadata.proto.v0 import schema_pb2

        schema = proto_utils.proto_text_to_better_proto(
            ProtoSchema(), path_or_proto_text, schema_pb2.Schema()
        )

        return TensorflowMetadata(schema)

    @classmethod
    def from_proto_text_file(cls, path: str, file_name="schema.pbtxt") -> "TensorflowMetadata":
        path = pathlib.Path(path) / file_name
        return cls.from_proto_text(_read_file(str(path)))

    def to_proto_text(self) -> str:
        from tensorflow_metadata.proto.v0 import schema_pb2

        return proto_utils.better_proto_to_proto_text(self.proto_schema, schema_pb2.Schema())

    def to_proto_text_file(self, path: str, file_name="schema.pbtxt") -> "TensorflowMetadata":
        return _write_file(self.to_proto_text(), path, file_name)

    def copy(self, **kwargs) -> "TensorflowMetadata":
        schema_copy = proto_utils.copy_better_proto_message(self.proto_schema, **kwargs)
        return TensorflowMetadata(schema_copy)

    @classmethod
    def from_merlin_schema(cls, schema: MerlinSchema):
        features = []
        for col_name, col_schema in schema.column_schemas.items():
            features.append(pb_feature(col_schema))

        proto_schema = ProtoSchema(feature=features)

        return TensorflowMetadata(proto_schema)

    def to_merlin_schema(self):
        merlin_schema = MerlinSchema()

        for feature in self.proto_schema.feature:
            col_schema = merlin_column(feature)
            merlin_schema.column_schemas[col_schema.name] = col_schema

        return merlin_schema


def pb_int_domain(column_schema):
    domain = column_schema.properties.get("domain")
    if domain is None:
        return None

    return IntDomain(
        name=column_schema.name,
        min=domain.get("min", None),
        max=domain.get("max", None),
        is_categorical=(
            Tags.CATEGORICAL in column_schema.tags or Tags.CATEGORICAL.value in column_schema.tags
        ),
    )


def pb_float_domain(column_schema):
    domain = column_schema.properties.get("domain")
    if domain is None:
        return None
    return FloatDomain(
        name=column_schema.name,
        min=domain.get("min", None),
        max=domain.get("max", None),
    )


def _dtype_name(column_schema):
    # TODO: Decide if we need this since we've standardized on numpy types
    if hasattr(column_schema.dtype, "kind"):
        return numpy.core._dtype._kind_name(column_schema.dtype)
    elif hasattr(column_schema.dtype, "item"):
        return type(column_schema.dtype(1).item()).__name__
    elif isinstance(column_schema.dtype, str):
        return column_schema.dtype
    elif hasattr(column_schema.dtype, "__name__"):
        return column_schema.dtype.__name__
    else:
        raise TypeError(f"unsupported dtype for column schema: {column_schema.dtype}")


def pb_extra_metadata(column_schema):
    properties = {k: v for k, v in column_schema.properties.items() if k != "domain"}
    properties["is_list"] = column_schema._is_list
    properties["is_ragged"] = column_schema._is_ragged

    msg_struct = Struct()
    any_pack = AnyPb2()
    json_formatted = json_format.ParseDict(properties, msg_struct)
    any_pack.Pack(json_formatted)

    bp_any = schema_bp.Any(any_pack.type_url, any_pack.value)

    return bp_any


def pb_tag(column_schema):
    return [tag.value if hasattr(tag, "value") else tag for tag in column_schema.tags]


def pb_feature(column_schema):
    feature = Feature(name=column_schema.name)

    feature = set_feature_domain(feature, column_schema)

    if column_schema._is_list:
        value_count = column_schema.properties.get("value_count", {})
        min_length = value_count.get("min")
        max_length = value_count.get("max")

        if min_length and max_length and min_length == max_length:
            feature.shape = FixedShape(min_length)
        elif min_length and max_length and min_length < max_length:
            feature.value_count = ValueCount(min=min_length, max=max_length)
        else:
            feature.value_count = ValueCount(min=0, max=0)

    feature.annotation.tag = pb_tag(column_schema)
    feature.annotation.extra_metadata = pb_extra_metadata(column_schema)

    return feature


def set_feature_domain(feature, column_schema):
    DOMAIN_CONSTRUCTORS = {
        FeatureType.INT: pb_int_domain,
        FeatureType.FLOAT: pb_float_domain,
    }

    pb_type = FEATURE_TYPES.get(_dtype_name(column_schema))
    if pb_type:
        feature.type = pb_type

        domain_attr = DOMAIN_ATTRS[pb_type]
        domain_fn = DOMAIN_CONSTRUCTORS[pb_type]
        domain = domain_fn(column_schema)
        if domain:
            setattr(feature, domain_attr, domain)

    return feature


def merlin_domain(feature):
    domain = {}

    domain_attr = DOMAIN_ATTRS.get(feature.type)

    if domain_attr and proto_utils.has_field(feature, domain_attr):
        domain_value = getattr(feature, domain_attr)
        if hasattr(domain_value, "min") and hasattr(domain_value, "max"):
            domain["min"] = domain_value.min
            domain["max"] = domain_value.max

    return domain


def merlin_properties(feature):
    extra_metadata = feature.annotation.extra_metadata

    if isinstance(extra_metadata, schema_bp.Any):
        msg_struct = Struct()
        msg_struct.ParseFromString(bytes(extra_metadata.value))
        properties = dict(msg_struct.items())

    elif len(extra_metadata) > 1:
        raise ValueError(
            f"{feature.name}: extra_metadata should have 1 item, has \
            {len(feature.annotation.extra_metadata)}"
        )
    elif len(extra_metadata) == 1:
        properties = feature.annotation.extra_metadata[0].value
    else:
        properties = {}

    domain = merlin_domain(feature)
    if domain:
        properties["domain"] = domain

    return properties


def merlin_dtype(feature):
    dtype = None
    if feature.type == FeatureType.INT:
        dtype = numpy.int
    elif feature.type == FeatureType.FLOAT:
        dtype = numpy.float

    return dtype


def merlin_column(feature):
    name = feature.name
    tags = list(feature.annotation.tag) or []
    properties = merlin_properties(feature)
    dtype = merlin_dtype(feature)

    is_list = properties.pop("is_list", False)
    is_ragged = properties.pop("is_ragged", False)

    return ColumnSchema(name, tags, properties, dtype, is_list, _is_ragged=is_ragged)


def _read_file(path: str):
    # TODO: Should we be using fsspec here too?
    path = pathlib.Path(path)
    if path.is_file():
        with open(path, "r") as f:
            contents = f.read()
    else:
        raise ValueError("Path is not file")

    return contents


def _write_file(contents: str, path: str, filename: str):
    fs = fsspec.get_fs_token_paths(path)[0]

    try:
        with fs.open(fs.sep.join([str(path), filename]), "w") as f:
            f.write(contents)
    except Exception as e:
        if not fs.isdir(path):
            raise ValueError(f"The path provided is not a valid directory: {path}") from e
        raise
