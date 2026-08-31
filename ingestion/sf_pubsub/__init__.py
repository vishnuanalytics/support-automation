"""Salesforce Pub/Sub API CDC subscriber (Phase 20l).

`pubsub_api_pb2*.py` are generated from the vendored `pubsub_api.proto`
(Salesforce Pub/Sub API v1). Regenerate after a proto bump with:

    python -m grpc_tools.protoc -Iingestion/sf_pubsub \
        --python_out=ingestion/sf_pubsub --grpc_python_out=ingestion/sf_pubsub \
        ingestion/sf_pubsub/pubsub_api.proto
    # then fix the grpc file's sibling import:
    sed -i 's/^import pubsub_api_pb2 as/from . import pubsub_api_pb2 as/' \
        ingestion/sf_pubsub/pubsub_api_pb2_grpc.py
"""
