import os
from pathlib import Path
from unittest.case import TestCase

import boto3
import pytest
import yaml
from samcli.lib.deploy.deployer import Deployer
from tests_integ.helpers.helpers import transform_template, verify_stack_resources, generate_suffix, create_bucket

STACK_NAME_PREFIX = "sam-integ-stack-"
S3_BUCKET_PREFIX = "sam-integ-bucket-"
CODE_KEY_TO_FILE_MAP = {"codeuri": "code.zip", "contenturi": "layer1.zip", "definitionuri": "swagger1.json"}


class BaseTest(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tests_integ_dir = Path(__file__).resolve().parents[1]
        cls.resources_dir = Path(cls.tests_integ_dir, "resources")
        cls.template_dir = Path(cls.resources_dir, "templates", "single")
        cls.output_dir = cls.tests_integ_dir
        cls.expected_dir = Path(cls.resources_dir, "expected", "single")
        code_dir = Path(cls.resources_dir, "code")

        cls.s3_bucket_name = S3_BUCKET_PREFIX + generate_suffix()
        session = boto3.session.Session()
        my_region = session.region_name
        create_bucket(cls.s3_bucket_name, region=my_region)

        cls.s3_client = boto3.client("s3")
        cls.code_key_to_url = {}

        for key, file_name in CODE_KEY_TO_FILE_MAP.items():
            code_path = str(Path(code_dir, file_name))
            cls.s3_client.upload_file(code_path, cls.s3_bucket_name, file_name)
            code_url = f"s3://{cls.s3_bucket_name}/{file_name}"
            cls.code_key_to_url[key] = code_url

    @classmethod
    def tearDownClass(cls):
        cls._clean_bucket()

    @classmethod
    def _clean_bucket(cls):
        response = cls.s3_client.list_objects_v2(Bucket=cls.s3_bucket_name)
        for content in response["Contents"]:
            cls.s3_client.delete_object(Key=content["Key"], Bucket=cls.s3_bucket_name)
        cls.s3_client.delete_bucket(Bucket=cls.s3_bucket_name)

    def setUp(self):
        self.cloudformation_client = boto3.client("cloudformation")
        self.deployer = Deployer(self.cloudformation_client, changeset_prefix="sam-integ-")

    def create_and_verify_stack(self, file_name):
        input_file_path = str(Path(self.template_dir, file_name + ".yaml"))
        self.output_file_path = str(Path(self.output_dir, "cfn_" + file_name + ".yaml"))
        expected_resource_path = str(Path(self.expected_dir, file_name + ".json"))
        self.stack_name = STACK_NAME_PREFIX + file_name.replace("_", "-") + generate_suffix()

        self.sub_input_file_path = self._update_template(input_file_path, file_name)
        transform_template(self.sub_input_file_path, self.output_file_path)
        self._deploy_stack()
        self._verify_stack(expected_resource_path)

    def tearDown(self):
        self.cloudformation_client.delete_stack(StackName=self.stack_name)
        if os.path.exists(self.output_file_path):
            os.remove(self.output_file_path)
        if os.path.exists(self.sub_input_file_path):
            os.remove(self.sub_input_file_path)

    def _update_template(self, input_file_path, file_name):
        updated_template_path = str(Path(self.output_dir, "sub_" + file_name + ".yaml"))
        with open(input_file_path, "r") as f:
            data = f.read()
        for key, s3_url in self.code_key_to_url.items():
            data = data.replace(f"${{{key}}}", s3_url)
        yaml_doc = yaml.load(data, Loader=yaml.FullLoader)

        with open(updated_template_path, "w") as f:
            yaml.dump(yaml_doc, f)

        return updated_template_path

    def _deploy_stack(self):
        with open(self.output_file_path, "r") as cfn_file:
            result, changeset_type = self.deployer.create_and_wait_for_changeset(
                stack_name=self.stack_name,
                cfn_template=cfn_file.read(),
                parameter_values=[],
                capabilities=["CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND"],
                role_arn=None,
                notification_arns=[],
                s3_uploader=None,
                tags=[],
            )
            self.deployer.execute_changeset(result["Id"], self.stack_name)
            self.deployer.wait_for_execute(self.stack_name, changeset_type)

    def _verify_stack(self, expected_resource_path):
        stacks_description = self.cloudformation_client.describe_stacks(StackName=self.stack_name)
        stack_resources = self.cloudformation_client.list_stack_resources(StackName=self.stack_name)
        # verify if the stack was successfully created
        self.assertEqual(stacks_description["Stacks"][0]["StackStatus"], "CREATE_COMPLETE")
        # verify if the stack contains the expected resources
        self.assertTrue(verify_stack_resources(expected_resource_path, stack_resources))
