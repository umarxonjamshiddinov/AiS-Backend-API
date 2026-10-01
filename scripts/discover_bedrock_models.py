"""Read-only Bedrock catalog discovery. Never invoke a model or print credentials."""

import json
import os
import sys
from contextlib import suppress

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import PROJECT_ROOT, get_settings


def main():
    settings = get_settings()
    report = {"region": settings.aws_region, "candidates": [], "operations": {}}
    token = settings.aws_bearer_token_bedrock
    if not token or not token.get_secret_value():
        print("Bedrock catalog: server bearer credential is not configured")
        return 2
    os.environ["AWS_BEARER_TOKEN_BEDROCK"] = token.get_secret_value()
    client = None
    try:
        client = boto3.client(
            "bedrock",
            region_name=settings.aws_region,
            config=Config(
                signature_version="bearer",
                connect_timeout=10,
                read_timeout=20,
                retries={"mode": "standard", "total_max_attempts": 1},
            ),
        )
        for operation, collection in (
            ("list_foundation_models", "modelSummaries"),
            ("list_inference_profiles", "inferenceProfileSummaries"),
        ):
            try:
                params = {}
                while True:
                    response = getattr(client, operation)(**params)
                    for model in response.get(collection, []):
                        name = model.get("modelName", model.get("inferenceProfileName", ""))
                        model_id = model.get("modelId", model.get("inferenceProfileId", ""))
                        searchable = (
                            name + " " + model_id + " " + json.dumps(model.get("models", []))
                        ).lower()
                        if "kimi" not in searchable:
                            continue
                        report["candidates"].append(
                            {
                                "name": name,
                                "id": model_id,
                                "source": operation,
                                "input_modalities": model.get("inputModalities"),
                                "output_modalities": model.get("outputModalities"),
                                "streaming_reported_by_provider": model.get(
                                    "responseStreamingSupported"
                                ),
                                "inference_types": model.get("inferenceTypesSupported"),
                                "profile_status": model.get("status"),
                                "is_kimi_k2_5": any(v in searchable for v in ("2.5", "2-5", "2_5")),
                            }
                        )
                    if not response.get("nextToken"):
                        break
                    params["nextToken"] = response["nextToken"]
                report["operations"][operation] = "ok"
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "provider_error")
                report["operations"][operation] = (
                    code
                    if code
                    in {
                        "AccessDeniedException",
                        "UnrecognizedClientException",
                        "ValidationException",
                        "ExpiredTokenException",
                        "ThrottlingException",
                        "ResourceNotFoundException",
                    }
                    else "provider_error"
                )
        for model in report["candidates"]:
            if model["source"] != "list_foundation_models" or not model["is_kimi_k2_5"]:
                continue
            try:
                availability = client.get_foundation_model_availability(modelId=model["id"])
                model["availability"] = {
                    key: availability.get(key)
                    for key in (
                        "authorizationStatus",
                        "entitlementAvailability",
                        "regionAvailability",
                    )
                }
            except ClientError:
                model["availability"] = "not_confirmed"
    except BotoCoreError:
        report["connection_status"] = "sdk_connection_failed"
    finally:
        if client is not None:
            with suppress(Exception):
                client.close()
    destination = PROJECT_ROOT / ".runtime" / "bedrock_catalog.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 3 if report.get("connection_status") else 0


if __name__ == "__main__":
    sys.exit(main())
