import logging

import pytest
import rhoas_kafka_instance_sdk
from constants import (
    CONSUMER_IMAGE,
    CONSUMER_POD,
    DEBEZIUM_NS,
    KAFKA_CLOUD_PROVIDER,
    KAFKA_NAME,
    KAFKA_PLAN,
    KAFKA_REGION,
    KAFKA_SA_NAME,
    KAFKA_TIMEOUT,
    KAFKA_TOPICS,
)
from ocp_resources.namespace import Namespace
from ocp_resources.pod import Pod
from ocp_resources.utils import TimeoutSampler
from ocp_utilities.infra import cluster_resource
from rhoas_kafka_instance_sdk.api import acls_api, topics_api
from rhoas_kafka_instance_sdk.model.acl_binding import AclBinding
from rhoas_kafka_instance_sdk.model.acl_operation import AclOperation
from rhoas_kafka_instance_sdk.model.acl_pattern_type import AclPatternType
from rhoas_kafka_instance_sdk.model.acl_permission_type import AclPermissionType
from rhoas_kafka_instance_sdk.model.acl_resource_type import AclResourceType
from rhoas_kafka_instance_sdk.model.config_entry import ConfigEntry
from rhoas_kafka_instance_sdk.model.new_topic_input import NewTopicInput
from rhoas_kafka_instance_sdk.model.topic_settings import TopicSettings
from rhoas_kafka_mgmt_sdk.model.kafka_request_payload import KafkaRequestPayload
from rhoas_service_accounts_mgmt_sdk.model.service_account_create_request_data import (
    ServiceAccountCreateRequestData,
)

from utilities.template_utils import get_resource_j2_template, render_yaml_from_dict


LOGGER = logging.getLogger(__name__)
WAIT_STATUS_TIMEOUT = 120


@pytest.fixture(scope="session")
def kafka_instance(kafka_mgmt_api_instance):
    _async = True
    kafka_request_payload = KafkaRequestPayload(
        cloud_provider=KAFKA_CLOUD_PROVIDER,
        name=KAFKA_NAME,
        region=KAFKA_REGION,
        plan=KAFKA_PLAN,
        reauthentication_enabled=True,
    )
    kafka_create_api = kafka_mgmt_api_instance.create_kafka(
        _async=_async, kafka_request_payload=kafka_request_payload
    )
    assert (
        kafka_create_api.status == "accepted"
    ), f"Failed to create a kafka instance. API response:\n{kafka_create_api}"

    kafka_status_samples = TimeoutSampler(
        wait_timeout=KAFKA_TIMEOUT,
        sleep=10,
        func=lambda: kafka_mgmt_api_instance.get_kafka_by_id(
            id=kafka_create_api.id
        ).status
        == "ready",
    )
    for sample in kafka_status_samples:
        if sample:
            break
    kafka_ready_api = kafka_mgmt_api_instance.get_kafka_by_id(id=kafka_create_api.id)
    LOGGER.info(f"Kafka instance is ready:\n{kafka_ready_api}")
    yield kafka_ready_api

    kafka_mgmt_api_instance.delete_kafka_by_id(
        async_req=True, _async=_async, id=kafka_ready_api.id
    )


@pytest.fixture(scope="session")
def kafka_instance_client(kafka_instance, access_token):
    # https://github.com/redhat-developer/app-services-sdk-python/tree/main/sdks/kafka_instance_sdk
    configuration = rhoas_kafka_instance_sdk.Configuration(
        host=kafka_instance.admin_api_server_url, access_token=access_token
    )

    with rhoas_kafka_instance_sdk.ApiClient(configuration=configuration) as api_client:
        yield api_client


@pytest.fixture(scope="session")
def kafka_instance_sa(kafka_instance_client, service_accounts_api_instance):
    service_account_create_request_data = ServiceAccountCreateRequestData(
        name=KAFKA_SA_NAME,
        description=f"{KAFKA_NAME} instance service-account",
    )
    kafka_sa = service_accounts_api_instance.create_service_account(
        service_account_create_request_data=service_account_create_request_data
    )
    assert (
        kafka_sa.id
    ), f"Failed to create service-account for kafka. API response:\n{kafka_sa}"
    LOGGER.info(f"kafka service-account:\n{kafka_sa}")

    # Binding the service-account instance to kafka with privileges
    # via AclBinding instance
    acl_api_instance = acls_api.AclsApi(api_client=kafka_instance_client)

    # acl binding attributes
    resources = ["GROUP", "TOPIC", "TRANSACTIONAL_ID"]
    resource_name = "*"
    pattern_type = AclPatternType("LITERAL")
    permission = AclPermissionType("ALLOW")
    principal = f"User:{kafka_sa.id}"
    operation = AclOperation("ALL")

    for resource in resources:
        resource_type = AclResourceType(resource)
        acl_binding = AclBinding(
            resource_type=resource_type,
            resource_name=resource_name,
            pattern_type=pattern_type,
            permission=permission,
            principal=principal,
            operation=operation,
        )
        acl_api_instance.create_acl(acl_binding=acl_binding)

        # validating current acl created
        kafka_sa_acl = acl_api_instance.get_acls(
            resource_type=resource_type,
            resource_name=resource_name,
            pattern_type=pattern_type,
            permission=permission,
            principal=principal,
            operation=operation,
            async_req=True,
        )
        # TODO: get data from ApplyResult object to assert current acl created
        assert kafka_sa_acl

    yield kafka_sa

    delete_sa_callback = service_accounts_api_instance.delete_service_account(
        id=kafka_sa.id, async_req=True
    )  # TODO: not deleting properly.
    LOGGER.info(delete_sa_callback.get())


@pytest.fixture(scope="session")
def kafka_topics(kafka_instance_client):
    kafka_topics_api_instance = topics_api.TopicsApi(api_client=kafka_instance_client)

    for topics_group in KAFKA_TOPICS:
        for topic in topics_group["topics"]:
            new_topic_input = NewTopicInput(
                name=topic,
                settings=TopicSettings(
                    num_partitions=topics_group["num_partitions"],
                    config=[
                        ConfigEntry(
                            key="cleanup.policy",
                            value=topics_group["cleanup_policy"],
                        ),
                    ],
                ),
            )
            kafka_topics_api_instance.create_topic(new_topic_input=new_topic_input)
    return kafka_topics


@pytest.fixture(scope="session")
def debezium_namespace(admin_client):
    with cluster_resource(Namespace)(client=admin_client, name=DEBEZIUM_NS) as dbz_ns:
        dbz_ns.wait_for_status(
            status=Namespace.Status.ACTIVE, timeout=WAIT_STATUS_TIMEOUT
        )
        yield dbz_ns


@pytest.fixture(scope="session")
def consumer_pod(admin_client, kafka_instance, kafka_instance_sa, debezium_namespace):
    pod_manifest_template = get_resource_j2_template(
        template_name="managed_services/mas_debezium/consumer_pod.j2"
    )
    pod_manifest_yaml = render_yaml_from_dict(
        template=pod_manifest_template,
        _dict={
            "debezium_namespace": debezium_namespace.name,
            "consumer_pod_name": CONSUMER_POD,
            "consumer_image": CONSUMER_IMAGE,
            "kafka_bootstrap_url": kafka_instance.bootstrap_server_host,
            "kafka_sa_client_id": kafka_instance_sa.id,
            "kafka_sa_client_secret": kafka_instance_sa.secret,
        },
    )

    with cluster_resource(Pod)(
        client=admin_client, yaml_file=pod_manifest_yaml
    ) as consumer:
        consumer.wait_for_status(status=Pod.Status.RUNNING, timeout=WAIT_STATUS_TIMEOUT)
        yield consumer
