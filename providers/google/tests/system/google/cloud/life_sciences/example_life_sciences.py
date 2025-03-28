#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from airflow.models.baseoperator import chain
from airflow.models.dag import DAG
from airflow.providers.google.cloud.operators.gcs import GCSCreateBucketOperator, GCSDeleteBucketOperator
from airflow.providers.google.cloud.operators.life_sciences import LifeSciencesRunPipelineOperator
from airflow.providers.google.cloud.transfers.local_to_gcs import LocalFilesystemToGCSOperator
from airflow.utils.trigger_rule import TriggerRule

from system.google import DEFAULT_GCP_SYSTEM_TEST_PROJECT_ID

ENV_ID = os.environ.get("SYSTEM_TESTS_ENV_ID", "default")
PROJECT_ID = os.environ.get("SYSTEM_TESTS_GCP_PROJECT") or DEFAULT_GCP_SYSTEM_TEST_PROJECT_ID
DAG_ID = "life_sciences"

BUCKET_NAME = f"bucket_{DAG_ID}-{ENV_ID}"

FILE_NAME = "file"
LOCATION = "us-central1"

CURRENT_FOLDER = Path(__file__).parent
FILE_LOCAL_PATH = str(Path(CURRENT_FOLDER) / "resources" / FILE_NAME)

# [START howto_configure_simple_action_pipeline]
SIMPLE_ACTION_PIPELINE = {
    "pipeline": {
        "actions": [
            {"imageUri": "bash", "commands": ["-c", "echo Hello, world"]},
        ],
        "resources": {
            "regions": [f"{LOCATION}"],
            "virtualMachine": {
                "machineType": "n1-standard-1",
            },
        },
    },
}
# [END howto_configure_simple_action_pipeline]

# [START howto_configure_multiple_action_pipeline]
MULTI_ACTION_PIPELINE = {
    "pipeline": {
        "actions": [
            {
                "imageUri": "google/cloud-sdk",
                "commands": ["gsutil", "cp", f"gs://{BUCKET_NAME}/{FILE_NAME}", "/tmp"],
            },
            {"imageUri": "bash", "commands": ["-c", "echo Hello, world"]},
            {
                "imageUri": "google/cloud-sdk",
                "commands": [
                    "gsutil",
                    "cp",
                    f"gs://{BUCKET_NAME}/{FILE_NAME}",
                    f"gs://{BUCKET_NAME}/output.in",
                ],
            },
        ],
        "resources": {
            "regions": [f"{LOCATION}"],
            "virtualMachine": {
                "machineType": "n1-standard-1",
            },
        },
    }
}
# [END howto_configure_multiple_action_pipeline]

with DAG(
    DAG_ID,
    schedule="@once",
    start_date=datetime(2021, 1, 1),
    catchup=False,
    tags=["example", "life-sciences"],
) as dag:
    create_bucket = GCSCreateBucketOperator(task_id="create_bucket", bucket_name=BUCKET_NAME)

    upload_file = LocalFilesystemToGCSOperator(
        task_id="upload_file",
        src=FILE_LOCAL_PATH,
        dst=FILE_NAME,
        bucket=BUCKET_NAME,
    )

    # [START howto_run_pipeline]
    simple_life_science_action_pipeline = LifeSciencesRunPipelineOperator(
        task_id="simple-action-pipeline",
        body=SIMPLE_ACTION_PIPELINE,
        project_id=PROJECT_ID,
        location=LOCATION,
    )
    # [END howto_run_pipeline]

    multiple_life_science_action_pipeline = LifeSciencesRunPipelineOperator(
        task_id="multi-action-pipeline", body=MULTI_ACTION_PIPELINE, project_id=PROJECT_ID, location=LOCATION
    )

    delete_bucket = GCSDeleteBucketOperator(
        task_id="delete_bucket", bucket_name=BUCKET_NAME, trigger_rule=TriggerRule.ALL_DONE
    )

    chain(
        # TEST SETUP
        create_bucket,
        upload_file,
        # TEST BODY
        simple_life_science_action_pipeline,
        multiple_life_science_action_pipeline,
        # TEST TEARDOWN
        delete_bucket,
    )

    from tests_common.test_utils.watcher import watcher

    # This test needs watcher in order to properly mark success/failure
    # when "tearDown" task with trigger rule is part of the DAG
    list(dag.tasks) >> watcher()


from tests_common.test_utils.system_tests import get_test_run  # noqa: E402

# Needed to run the example DAG with pytest (see: tests/system/README.md#run_via_pytest)
test_run = get_test_run(dag)
