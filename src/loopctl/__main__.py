import argparse
import asyncio
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
import os
from typing import Dict, Generator, List, Tuple
from urllib.parse import urlparse
import requests
import itertools
import re

import fire
import yaml
import httpx

@dataclass(frozen=True)
class ResourceReference:
    kind: str
    name: str

    def __str__(self):
        return f"{self.kind}/{self.name}"

@dataclass(frozen=True)
class LocatedResource:
    path: str
    index: int
    resource: "Resource"

@dataclass(frozen=True)
class ResourceId:
    kind: str
    id: str

    def __str__(self):
        return f"{self.kind}/{self.id}"

@dataclass(frozen=True)
class ConfigKey:
    reference: ResourceReference
    key: str

skipped_apiversion = []
skipped_kind = []

existing_resources: Dict[ResourceReference, LocatedResource] = {}
compiled_resources: Dict[ResourceReference, "Resource"] = {}
generated_values: Dict[ConfigKey, str] = {}
id_to_reference: Dict[ResourceId, ResourceReference] = {}


CONFIG_PRIORITY = {
    "ResourceDefs": 0,
    "ApiKey": 1,
    "Client": 2,
    "Group": 3,
    "Loop": 4,
    "Stream": 5,
    "Cluster": 6,
}


def read_resource_configs_file(file, read_fully=False):
    global skipped_apiversion, skipped_kind

    try:
        with open(file, "r") as f:
            configs = list(yaml.safe_load_all(f.read()))
    except Exception as e:
        print(f"Failed to read file {file}:", e.args)
        if read_fully:
            raise e
        return []

    for i, config in enumerate(configs):
        if read_fully:
            yield i, config
            continue

        if "apiVersion" not in config:
            print(f"Config in file {file} is missing apiVersion")
            continue
        if not isinstance(config["apiVersion"], str):
            print(
                f"Config in file {file} has invalid apiVersion. It should be a string."
            )
            continue
        if not re.match(r"^[a-z0-9.-]+/[a-z0-9.-]+$", config["apiVersion"]):
            print(
                f"Config in file {file} has invalid apiVersion. It should be in the format group/version. Group and version should be lowercase alphanumeric characters, dots or dashes."
            )
            continue
        if "kind" not in config:
            print(f"Config in file {file} is missing kind")
            continue
        if not isinstance(config["kind"], str):
            print(f"Config in file {file} has invalid kind. It should be a string.")
            continue
        if "metadata" not in config:
            print(f"Config in file {file} is missing metadata")
            continue
        if not isinstance(config["metadata"], dict):
            print(f"Config in file {file} has invalid metadata. It should be a dict.")
            continue
        if "name" not in config["metadata"]:
            print(f"Config in file {file} is missing metadata.name")
            continue
        if not isinstance(config["metadata"]["name"], str):
            print(
                f"Config in file {file} has invalid metadata.name. It should be a string."
            )
            continue

        if config["apiVersion"] != "thatone.ai/v1":
            print(f"Skipping config in {file} with apiVersion {config['apiVersion']}.")
            skipped_apiversion.append(file)
            continue
        if config["kind"] not in CONFIG_PRIORITY.keys():
            print(f"Skipping config in {file} with kind {config['kind']}.")
            skipped_kind.append(file)
            continue

        yield i, config


def read_resource_configs_dir(dirpath, read_fully=False) -> List[Tuple[str, dict]]:
    """Read the given file and return its contents."""
    dirpath = os.path.realpath(dirpath)
    if os.path.isfile(dirpath):
        return [
            (dirpath, i, x)
            for i, x in read_resource_configs_file(dirpath, read_fully=read_fully)
        ]

    result = []
    for root, dirs, files in os.walk(dirpath):
        for filepath in files:
            if filepath.endswith(".yaml"):
                fullpath = os.path.join(root, filepath)
                config_files = [
                    [fullpath, i, x]
                    for i, x in read_resource_configs_file(
                        fullpath, read_fully=read_fully
                    )
                ]
                result.extend(config_files)

    return result


def config_priority(config):
    """Return the priority of the given config."""
    return CONFIG_PRIORITY[config["kind"]]


def create_resource_path(folder, config):
    """Create the path for the given resource."""
    prefix = os.path.join(folder, config["kind"], config["metadata"]["name"])
    suffix = 0
    while True:
        result = prefix + (suffix or "") + ".yaml"
        if not os.path.exists(result):
            return result
        suffix += 1


class DeferredValue:
    def __str__(self):
        return self.link()


def random_id():
    random_bytes = os.urandom(20)
    return "".join("{:02x}".format(byte) for byte in random_bytes)


def get_generated_id(configKey: ConfigKey, prefix) -> str:
    created = compiled_resources.get(configKey.reference)
    if created != None:
        result = created.config["spec"].get(configKey)
        if result and not isinstance(result, DeferredValue):
            return result

    generated = generated_values.get(configKey)
    if generated:
        return generated

    existing = existing_resources.get(configKey.reference)
    if existing:
        result = existing.resource.config["spec"].get(configKey)
        if result:
            return result

    new_id = f"{prefix}{random_id()}"
    generated_values[configKey] = new_id
    return new_id


def get_resource_by_reference(reference: ResourceReference):
    created = compiled_resources.get(reference)
    if created:
        return created
    
    existing = existing_resources.get(reference)
    if existing:
        return existing.resource

def require_resource_by_reference(reference: ResourceReference):
    resource = get_resource_by_reference(reference)
    if resource == None:
        print("Error: Could not find resource", reference)
        raise ValueError()
    return resource

class DeferredId(DeferredValue):
    def __init__(self, kind, name, field):
        reference = ResourceReference(kind, name)
        self.configKey = ConfigKey(reference, field)

    def link(self):
        global id_to_reference
        result = get_generated_id(self.configKey, f"{self.configKey.reference.name}-")

        id = ResourceId(self.configKey.reference.kind, result)
        id_to_reference[id] = self.configKey.reference

        return result
    

class ApiKeyId(DeferredId):
    def __init__(self, name):
        reference = ResourceReference("ApiKey", name)
        self.configKey = ConfigKey(reference, "apiKey")


    def link(self):
        return get_generated_id(self.configKey, "")


class ClientId(DeferredId):
    def __init__(self, name):
        super().__init__("Client", name, "clientId")


class GroupId(DeferredId):
    def __init__(self, name):
        super().__init__("Group", name, "groupId")


class LoopId(DeferredId):
    def __init__(self, name):
        super().__init__("Loop", name, "loopId")


class StreamId(DeferredId):
    def __init__(self, name):
        super().__init__("Stream", name, "streamId")


class ClusterId(DeferredId):
    def __init__(self, name):
        super().__init__("Cluster", name, "clusterId")


class Remote(DeferredValue):
    def __init__(self, resourceKey, remote=None):
        self.resource_reference = resourceKey
        self.remote = remote


    def link(self):
        if self.remote:
            return self.remote

        resource = require_resource_by_reference(self.resource_reference)
        while True:
            # Return the namespace if it's known
            namespace = resource.namespace
            if namespace:
                if isinstance(namespace, str):
                    return namespace
                if isinstance(namespace, Remote) and namespace.remote:
                    return namespace.remote

            # Otherwise, go up a level
            if resource.parent == None:
                print("Error: Could not find parent for", resource.reference)
                print("new resources:", compiled_resources.keys())
                raise ValueError()
            resource = resource.parent


def validate_keys(location: str, config: dict, required_keys: str, optional_keys=set()):
    """Validate that the given config has the required keys and no other keys."""
    keys = set(config.keys())
    if not keys >= required_keys:
        print(f"Error: Missing required keys in {location}: {required_keys - keys}")
        return False
    if not keys <= required_keys | optional_keys:
        print(
            f"Error: Unknown keys in {location}: {keys - required_keys - optional_keys}"
        )
        return False
    return True


def link(value):
    if isinstance(value, DeferredValue):
        return value.link()
    elif isinstance(value, list):
        return [link(x) for x in value]
    elif isinstance(value, dict):
        return {k: link(v) for k, v in value.items()}
    elif isinstance(value, Resource):
        return value.link()
    else:
        return value


class Resource:
    config: dict
    parent_ref_or_id: tuple = None
    kind: str
    name: str

    @property
    def namespace(self):
        return self.config["metadata"].get("namespace")
    
    @property
    def api_version(self):
        return self.config["apiVersion"]
    
    
    @property
    def reference(self):
        return ResourceReference(self.kind, self.name)
    
    @property
    def parent(self):
        if self.parent_ref_or_id == None:
            return None

        if isinstance(self.parent_ref_or_id, ResourceReference):
            return get_resource_by_reference(self.parent_ref_or_id)
        
        if isinstance(self.parent_ref_or_id, ResourceId):
            reference = id_to_reference[self.parent_ref_or_id]
            return get_resource_by_reference(reference)
        
        raise ValueError(f'Invalid parent reference {self.parent_ref_or_id}')


    def apply(self):
        # Find all applicable API keys
        api_keys = apikeys_for_resource(self)
        
        url = str(self.namespace)
        compiled_config = self.link()

        response = None
        for apikey in api_keys:
            response = put_with_key(
                f"{url}/policies/{self.api_version}/{self.kind}",
                compiled_config["spec"],
                apikey.key,
            )

            if response.status_code != 401:
                break

            print('Error: API key', apikey, 'is not authorized to apply', self.name)
            apikey.valid = False
        else:
            response = put_with_key(
                f"{url}/policies/{self.api_version}/{self.kind}",
                compiled_config["spec"]
            )

        if response == None:
            print(f'Error: no API keys are authorized to apply {self.reference}')
            return False
            
        elif response.status_code != 200:
            print('- Error: failed to apply', self.reference, response.status_code, response.text)
            return False
        
        print('applied', self.reference)
        return True

    def link(self):
        compiled_config = link(self.config)
        result = {
            "apiVersion": compiled_config["apiVersion"],
            "kind": compiled_config["kind"],
            "metadata": compiled_config["metadata"],
            "spec": {x: y for x, y in compiled_config["spec"].items() if y},
        }
        return result

def apikeys_for_resource(client_resource: Resource):
    while True:
        if isinstance(client_resource, Client):
            resource_id = ResourceId('Client', str(client_resource.config['spec']['clientId']))
            return apikeys_for_client(resource_id)
        if client_resource.parent == None:
            break
        client_resource = client_resource.parent

def get_resources_by_type(kind):
    for candidate, resource in compiled_resources.items():
        if candidate.kind == kind:
            yield resource

    for candidate, value in existing_resources.items():
        if candidate.kind == kind:
            yield value.resource


def apikeys_for_client(client_id: ResourceId) -> Generator['ApiKey', None, None]:
    print('getting keys for client', client_id)
    for apikey in get_resources_by_type('ApiKey'):
        if not apikey.valid:
            continue
        
        config = apikey.config
        if str(config['spec']['clientId']) != client_id.id:
            continue

        print('found key', apikey.key, 'for client', client_id)
        yield apikey
    
    parent_id = get_client_parent_id(client_id)
    if parent_id == None:
        print('could not find apikeys for parent', client_id)
        return
    
    print('parent id', parent_id)
    for apikey in apikeys_for_client(parent_id):
        yield apikey
    

CLIENT = None
def get_client() -> httpx.Client:
    global CLIENT
    if CLIENT == None:
        CLIENT = httpx.Client()
    return CLIENT

def put_with_key(url, json, key=None):
    client = get_client()
    if key:
        params = {'apikey': key}
    else:
        params = {}
    response = client.put(url, json=json, params=params)
    print(response)
    return response

class ApiKey(Resource):
    valid: bool = True

    @property
    def key(self):
        return str(self.config['spec']['apiKey'])

    def __init__(self, kind, config):
        self.kind = 'ApiKey'

        if kind == "ResourceDefs":
            if not validate_keys("apikey", config, {"name", "clientName"}):
                raise ValueError()

            self.name = config["name"]
            self.parent_ref_or_id = ResourceReference("Client", config["clientName"])

            self.config = {
                "apiVersion": "thatone.ai/v1",
                "kind": "ApiKey",
                "metadata": {
                    "name": config["name"],
                    "namespace": Remote(resourceKey=self.reference),
                },
                "spec": {
                    "clientId": ClientId(config["clientName"]),
                    "apiKey": ApiKeyId(config["name"]),
                },
            }
        elif kind == "ApiKey":
            if "namespace" not in config["metadata"]:
                print("Error: ApiKey is missing namespace")
                raise ValueError()
            if not validate_keys(
                "apikey", config.get("spec", {}), {"clientId", "apiKey"}
            ):
                raise ValueError()
            self.config = config
            self.name = config["metadata"]["name"]
    
    def attach_to_client(self, existing_key):
        config = self.link()
        print('apply', config['metadata']['name'])
        client = get_client()
        url = str(config['metadata']['namespace'])
        put_with_key(
            f"{url}/policies/thatone.ai/v1/ApiKey",
            config["spec"],
            existing_key,
        )
        # if response.status_code != 200:
            # print('Error: failed to attach key to client', response.status_code, response.text)
            # return False

        return True


    def apply(self):
        resource_id = ResourceId('Client', str(self.config['spec']['clientId']))
        for apikey in apikeys_for_client(resource_id):
            if self.attach_to_client(apikey.key):
                return True
            else:
                apikey.valid = False
                location = existing_resources[apikey.reference].path
                print('Warning: invalid API key', apikey.name, 'in', location)
                print("- Unless you're waiting for it to become active, you should delete it.")
        
        key = str(self.config['spec']['apiKey'])
        if not self.attach_to_client(key):
            print('None of the existing API keys are authorized to attach the key', self.config['metadata']['name'])
        
        return False

def get_client_parent_id(client_id: ResourceId):
    for key, value in id_to_reference.items():
        print(key, value)
    client_reference = id_to_reference.get(client_id)
    if client_reference == None:
        print('could not find client reference for id', client_id)
        return None
    
    resource = get_resource_by_reference(client_reference)
    if resource == None:
        print('could not find resource for client', client_reference)
        return None
    
    config = resource.config['spec']
    print('got config:', config)
    if 'parentId' in config:
        print('found parent id', config['parentId'])
        return ResourceId('Client', str(config['parentId']))
    
    return None

class Client(Resource):
    def __init__(self, kind, config):
        self.kind = 'Client'

        if kind == "ResourceDefs":
            if not validate_keys(
                "client",
                config,
                {"name"},
                {
                    "remote",
                    "parentName",
                    "dacTags",
                    "dacDefaultRead",
                    "dacDefaultWrite",
                    "dacDefaultExecute",
                    "dacWhitelistRead",
                    "dacWhitelistWrite",
                    "dacWhitelistExecute",
                    "dacBlacklistRead",
                    "dacBlacklistWrite",
                    "dacBlacklistExecute",
                    "macTags",
                    "macWhitelistRead",
                    "macWhitelistWrite",
                    "macWhitelistExecute",
                    "macBlacklistRead",
                    "macBlacklistWrite",
                    "macBlacklistExecute",
                },
            ):
                raise ValueError()

            self.name = config["name"]
            if "parentName" in config:
                self.parent_ref_or_id = ResourceReference("Client", config["parentName"])

            self.config = {
                "apiVersion": "thatone.ai/v1",
                "kind": "Client",
                "metadata": {
                    "name": config["name"],
                    "namespace": Remote(resourceKey=self.reference, remote=config.get("remote")),
                },
                "spec": {
                    "clientId": ClientId(config["name"]),
                    "parentId": ClientId(config.get("parentName"))
                    if "parentName" in config
                    else None,
                    "dacTags": config.get("dacTags", []),
                    "dacDefaultRead": [
                        GroupId(x) for x in config.get("dacDefaultRead", [])
                    ],
                    "dacDefaultWrite": [
                        GroupId(x) for x in config.get("dacDefaultWrite", [])
                    ],
                    "dacDefaultExecute": [
                        GroupId(x) for x in config.get("dacDefaultExecute", [])
                    ],
                    "dacWhitelistRead": [
                        GroupId(x) for x in config.get("dacWhitelistRead", [])
                    ],
                    "dacWhitelistWrite": [
                        GroupId(x) for x in config.get("dacWhitelistWrite", [])
                    ],
                    "dacWhitelistExecute": [
                        GroupId(x) for x in config.get("dacWhitelistExecute", [])
                    ],
                    "dacBlacklistRead": [
                        GroupId(x) for x in config.get("dacBlacklistRead", [])
                    ],
                    "dacBlacklistWrite": [
                        GroupId(x) for x in config.get("dacBlacklistWrite", [])
                    ],
                    "dacBlacklistExecute": [
                        GroupId(x) for x in config.get("dacBlacklistExecute", [])
                    ],
                    "macTags": config.get("macTags", []),
                    "macWhitelistRead": [
                        GroupId(x) for x in config.get("macWhitelistRead", [])
                    ],
                    "macWhitelistWrite": [
                        GroupId(x) for x in config.get("macWhitelistWrite", [])
                    ],
                    "macWhitelistExecute": [
                        GroupId(x) for x in config.get("macWhitelistExecute", [])
                    ],
                    "macBlacklistRead": [
                        GroupId(x) for x in config.get("macBlacklistRead", [])
                    ],
                    "macBlacklistWrite": [
                        GroupId(x) for x in config.get("macBlacklistWrite", [])
                    ],
                    "macBlacklistExecute": [
                        GroupId(x) for x in config.get("macBlacklistExecute", [])
                    ],
                },
            }
        elif kind == "Client":
            if "namespace" not in config["metadata"]:
                print("Error: Client is missing namespace")
                raise ValueError()

            if not validate_keys(
                "Client",
                config.get("spec", {}),
                {"clientId"},
                {
                    "parentId",
                    "dacTags",
                    "dacDefaultRead",
                    "dacDefaultWrite",
                    "dacDefaultExecute",
                    "dacWhitelistRead",
                    "dacWhitelistWrite",
                    "dacWhitelistExecute",
                    "dacBlacklistRead",
                    "dacBlacklistWrite",
                    "dacBlacklistExecute",
                    "macTags",
                    "macWhitelistRead",
                    "macWhitelistWrite",
                    "macWhitelistExecute",
                    "macBlacklistRead",
                    "macBlacklistWrite",
                    "macBlacklistExecute",
                },
            ):
                raise ValueError()
            self.config = config
            self.name = config["metadata"]["name"]
            resource_id = ResourceId('Client', str(config['spec']['clientId']))
            id_to_reference[resource_id] = self.reference
            
            parent_id = config['spec'].get('parentId')
            if parent_id:
                self.parent_ref_or_id = ResourceId('Client', str(parent_id))



class Group(Resource):
    def __init__(self, kind, config):
        self.kind = 'Group'

        if kind == "ResourceDefs":
            if not validate_keys(
                "group",
                config,
                {"name", "owner"},
                {
                    "public",
                    "includeClients",
                    "includeOrgs",
                    "includeGroups",
                    "excludeClients",
                    "excludeOrgs",
                    "excludeGroups",
                },
            ):
                raise ValueError

            self.name = config["name"]
            self.parent_ref_or_id = ResourceReference("Client", config["owner"])

            self.config = {
                "apiVersion": "thatone.ai/v1",
                "kind": "Group",
                "metadata": {
                    "name": config["name"],
                    "namespace": Remote(resourceKey=self.reference),
                },
                "spec": {
                    "ownerId": ClientId(config["owner"]),
                    "groupId": GroupId(config["name"]),
                    "public": config.get("public", False),
                    "includeClients": [
                        ClientId(x) for x in config.get("includeClients", [])
                    ],
                    "includeOrgs": [ClientId(x) for x in config.get("includeOrgs", [])],
                    "includeGroups": [
                        GroupId(x) for x in config.get("includeGroups", [])
                    ],
                    "excludeClients": [
                        ClientId(x) for x in config.get("excludeClients", [])
                    ],
                    "excludeOrgs": [ClientId(x) for x in config.get("excludeOrgs", [])],
                    "excludeGroups": [
                        GroupId(x) for x in config.get("excludeGroups", [])
                    ],
                },
            }
        elif kind == "Group":
            if "namespace" not in config["metadata"]:
                print("Error: Group is missing namespace")
                raise ValueError()
            if not validate_keys(
                "Group",
                config.get("spec", {}),
                {"ownerId", "groupId"},
                {
                    "public",
                    "includeClients",
                    "includeOrgs",
                    "includeGroups",
                    "excludeClients",
                    "excludeOrgs",
                    "excludeGroups",
                },
            ):
                raise ValueError
            self.config = config
            self.name = config["metadata"]["name"]     
            resource_id = ResourceId('Group', str(config['spec']['groupId']))
            id_to_reference[resource_id] = self.reference      
            self.parent_ref_or_id = ResourceId('Client', str(config['spec']['ownerId']))


class Loop(Resource):
    def __init__(self, kind, config):
        self.kind = 'Loop'
        if kind == "ResourceDefs":
            if not validate_keys(
                "loop",
                config,
                {"name", "owner"},
                {
                    "dacDefaultRead",
                    "dacDefaultWrite",
                    "dacDefaultExecute",
                    "dacWhitelistRead",
                    "dacWhitelistWrite",
                    "dacWhitelistExecute",
                    "dacBlacklistRead",
                    "dacBlacklistWrite",
                    "dacBlacklistExecute",
                },
            ):
                raise ValueError()

            self.name = config["name"]
            self.parent_ref_or_id = ResourceReference("Client", config["owner"])

            self.config = {
                "apiVersion": "thatone.ai/v1",
                "kind": "Loop",
                "metadata": {
                    "name": config["name"],
                    "namespace": Remote(resourceKey=self.reference),
                },
                "spec": {
                    "ownerId": ClientId(config["owner"]),
                    "loopId": LoopId(config["name"]),
                    "dacDefaultRead": [
                        GroupId(x) for x in config.get("dacDefaultRead", [])
                    ],
                    "dacDefaultWrite": [
                        GroupId(x) for x in config.get("dacDefaultWrite", [])
                    ],
                    "dacDefaultExecute": [
                        GroupId(x) for x in config.get("dacDefaultExecute", [])
                    ],
                    "dacWhitelistRead": [
                        GroupId(x) for x in config.get("dacWhitelistRead", [])
                    ],
                    "dacWhitelistWrite": [
                        GroupId(x) for x in config.get("dacWhitelistWrite", [])
                    ],
                    "dacWhitelistExecute": [
                        GroupId(x) for x in config.get("dacWhitelistExecute", [])
                    ],
                    "dacBlacklistRead": [
                        GroupId(x) for x in config.get("dacBlacklistRead", [])
                    ],
                    "dacBlacklistWrite": [
                        GroupId(x) for x in config.get("dacBlacklistWrite", [])
                    ],
                    "dacBlacklistExecute": [
                        GroupId(x) for x in config.get("dacBlacklistExecute", [])
                    ],
                },
            }
        elif kind == "Loop":
            if "namespace" not in config["metadata"]:
                print("Error: Loop is missing metadata.namespace")
                raise ValueError()
            if not validate_keys(
                "loop",
                config.get("spec", {}),
                {"ownerId", "loopId"},
                {
                    "dacDefaultRead",
                    "dacDefaultWrite",
                    "dacDefaultExecute",
                    "dacWhitelistRead",
                    "dacWhitelistWrite",
                    "dacWhitelistExecute",
                    "dacBlacklistRead",
                    "dacBlacklistWrite",
                    "dacBlacklistExecute",
                },
            ):
                raise ValueError()
            self.config = config
            self.name = config["metadata"]["name"]
            resource_id = ResourceId('Loop', str(config['spec']['loopId']))
            id_to_reference[resource_id] = self.reference
            self.parent_ref_or_id = ResourceId('Client', str(config['spec']['ownerId']))


class Stream(Resource):
    def __init__(self, kind, config):
        self.kind = 'Stream'
        if kind == "ResourceDefs":
            if not validate_keys(
                "stream",
                config,
                {"name", "loop"},
                {
                    "dacWhitelistRead",
                    "dacWhitelistWrite",
                    "dacWhitelistExecute",
                    "dacBlacklistRead",
                    "dacBlacklistWrite",
                    "dacBlacklistExecute",
                },
            ):
                raise ValueError()

            self.name = config["name"]
            self.parent_ref_or_id = ResourceReference("Loop", config["loop"])

            self.config = {
                "apiVersion": "thatone.ai/v1",
                "kind": "Stream",
                "metadata": {
                    "name": config["name"],
                    "namespace": Remote(resourceKey=self.reference),
                },
                "spec": {
                    "loopId": LoopId(config["loop"]),
                    "streamId": StreamId(config["name"]),
                    "dacWhitelistRead": [
                        GroupId(x) for x in config.get("dacWhitelistRead", [])
                    ],
                    "dacWhitelistWrite": [
                        GroupId(x) for x in config.get("dacWhitelistWrite", [])
                    ],
                    "dacWhitelistExecute": [
                        GroupId(x) for x in config.get("dacWhitelistExecute", [])
                    ],
                    "dacBlacklistRead": [
                        GroupId(x) for x in config.get("dacBlacklistRead", [])
                    ],
                    "dacBlacklistWrite": [
                        GroupId(x) for x in config.get("dacBlacklistWrite", [])
                    ],
                    "dacBlacklistExecute": [
                        GroupId(x) for x in config.get("dacBlacklistExecute", [])
                    ],
                },
            }
        elif kind == "Stream":
            if "namespace" not in config["metadata"]:
                print("Error: Stream is missing metadata.namespace")
                raise ValueError()
            if not validate_keys(
                "stream",
                config.get("spec", {}),
                {"loopId", "streamId"},
                {
                    "dacWhitelistRead",
                    "dacWhitelistWrite",
                    "dacWhitelistExecute",
                    "dacBlacklistRead",
                    "dacBlacklistWrite",
                    "dacBlacklistExecute",
                },
            ):
                raise ValueError()
            self.config = config
            self.name = config["metadata"]["name"]
            resource_id = ResourceId('Stream', str(config['spec']['streamId']))
            id_to_reference[resource_id] = self.reference
            self.parent_ref_or_id = ResourceId('Loop', str(config['spec']['loopId']))
            


class Cluster(Resource):
    def __init__(self, kind, config):
        self.kind = 'Cluster'
        if kind == "ResourceDefs":
            if not validate_keys(
                "cluster",
                config,
                {"name", "owner"},
                {
                    "dacDefaultRead",
                    "dacDefaultWrite",
                    "dacDefaultExecute",
                    "dacWhitelistRead",
                    "dacWhitelistWrite",
                    "dacWhitelistExecute",
                    "dacBlacklistRead",
                    "dacBlacklistWrite",
                    "dacBlacklistExecute",
                },
            ):
                raise ValueError()

            self.name = config["name"]
            self.parent_ref_or_id = ResourceReference("Client", config["owner"])

            self.config = {
                "apiVersion": "thatone.ai/v1",
                "kind": "Cluster",
                "metadata": {
                    "name": config["name"],
                    "namespace": Remote(resourceKey=self.reference),
                },
                "spec": {
                    "ownerId": ClientId(config["owner"]),
                    "clusterId": ClusterId(config["name"]),
                    "dacDefaultRead": [
                        GroupId(x) for x in config.get("dacDefaultRead", [])
                    ],
                    "dacDefaultWrite": [
                        GroupId(x) for x in config.get("dacDefaultWrite", [])
                    ],
                    "dacDefaultExecute": [
                        GroupId(x) for x in config.get("dacDefaultExecute", [])
                    ],
                    "dacWhitelistRead": [
                        GroupId(x) for x in config.get("dacWhitelistRead", [])
                    ],
                    "dacWhitelistWrite": [
                        GroupId(x) for x in config.get("dacWhitelistWrite", [])
                    ],
                    "dacWhitelistExecute": [
                        GroupId(x) for x in config.get("dacWhitelistExecute", [])
                    ],
                    "dacBlacklistRead": [
                        GroupId(x) for x in config.get("dacBlacklistRead", [])
                    ],
                    "dacBlacklistWrite": [
                        GroupId(x) for x in config.get("dacBlacklistWrite", [])
                    ],
                    "dacBlacklistExecute": [
                        GroupId(x) for x in config.get("dacBlacklistExecute", [])
                    ],
                },
            }
        elif kind == "Cluster":
            if "namespace" not in config["metadata"]:
                print("Error: Cluster is missing metadata.namespace")
                raise ValueError()
            if not validate_keys(
                "cluster",
                config.get("spec", {}),
                {"ownerId", "clusterId"},
                {
                    "dacDefaultRead",
                    "dacDefaultWrite",
                    "dacDefaultExecute",
                    "dacWhitelistRead",
                    "dacWhitelistWrite",
                    "dacWhitelistExecute",
                    "dacBlacklistRead",
                    "dacBlacklistWrite",
                    "dacBlacklistExecute",
                },
            ):
                raise ValueError()
            self.config = config
            self.name = config["metadata"]["name"]
            resource_id = ResourceId('Cluster', str(config['spec']['clusterId']))
            id_to_reference[resource_id] = self.reference
            self.parent_ref_or_id = ResourceId('Client', str(config['spec']['ownerId']))


def compile_resourcedefs(config):
    """Apply the given resource config."""
    items = config["spec"]
    error = False

    if not validate_keys(
        "spec",
        items,
        set(),
        {"apikeys", "groups", "clients", "loops", "streams", "clusters"},
    ):
        error = True

    for apikey in items.get("apikeys", []):
        try:
            yield ApiKey("ResourceDefs", apikey)
        except ValueError as e:
            error = True
            continue

    for client in items.get("clients", []):
        try:
            yield Client("ResourceDefs", client)
        except ValueError as e:
            error = True
            continue

    for group in items.get("groups", []):
        try:
            yield Group("ResourceDefs", group)
        except ValueError as e:
            error = True
            continue

    for loop in items.get("loops", []):
        try:
            yield Loop("ResourceDefs", loop)
        except ValueError as e:
            error = True
            continue

    for stream in items.get("streams", []):
        try:
            yield Stream("ResourceDefs", stream)
        except ValueError as e:
            error = True
            continue

    for cluster in items.get("clusters", []):
        try:
            yield Cluster("ResourceDefs", cluster)
        except ValueError as e:
            error = True
            continue

    if error:
        raise ValueError()


def compile_apikey(config):
    """Apply the given api key."""
    return ApiKey("ApiKey", config)


def compile_group(config):
    """Apply the given group."""
    return Group("Group", config)


def compile_client(config):
    """Apply the given client."""
    return Client("Client", config)


def compile_loop(config):
    """Apply the given loop."""
    return Loop("Loop", config)


def compile_stream(config):
    """Apply the given stream."""
    return Stream("Stream", config)


def compile_cluster(config):
    """Apply the given cluster."""
    return Cluster("Cluster", config)


def compile_config(filepath: str, config: dict) -> List[Resource]:
    """Apply the given config."""
    kind = config["kind"]

    if kind == "ResourceDefs":
        return list(compile_resourcedefs(config))
    elif kind == "ApiKey":
        return [compile_apikey(config)]
    elif kind == "Group":
        return [compile_group(config)]
    elif kind == "Client":
        return [compile_client(config)]
    elif kind == "Loop":
        return [compile_loop(config)]
    elif kind == "Stream":
        return [compile_stream(config)]
    elif kind == "Cluster":
        return [compile_cluster(config)]
    else:
        print("Unknown config kind", kind)
        raise ValueError(f"Unknown config kind {kind} in {filepath}")


def is_itl_resource(config):
    if "apiVersion" not in config or "kind" not in config:
        return False
    if config["apiVersion"] != "thatone.ai/v1":
        return False
    if config["kind"] not in CONFIG_PRIORITY.keys():
        return False

    return True


def expand_keys(config):
    if config["kind"] != "ResourceDefs":
        yield (config["kind"], config["metadata"]["name"])
        return

    for config_type, config_value in config["spec"].items():
        if config_type == "apikeys":
            for apikey in config_value:
                yield ("ApiKey", apikey["name"])
        elif config_type == "clients":
            for client in config_value:
                yield ("Client", client["name"])
        elif config_type == "groups":
            for group in config_value:
                yield ("Group", group["name"])
        elif config_type == "loops":
            for loop in config_value:
                yield ("Loop", loop["name"])
        elif config_type == "streams":
            for stream in config_value:
                yield ("Stream", stream["name"])
        elif config_type == "clusters":
            for cluster in config_value:
                yield ("Cluster", cluster["name"])
        else:
            raise ValueError(f"Unknown config type {config_type}")


def check_duplicates(configs):
    error = False

    known_keys = defaultdict(lambda: defaultdict(int))
    for filepath, i, config in configs:
        if not is_itl_resource(config):
            continue

        for key in expand_keys(config):
            known_keys[key][filepath] += 1

    for key, file_counts in known_keys.items():
        if sum(file_counts.values()) > 1:
            error = True
            files_str = "\n  ".join(file_counts.keys())
            print(f"Error: Duplicate config {key[0]}/{key[1]} in files:\n  {files_str}")

    if error:
        print(
            "You can either remove each duplicate or apply the configs one at a time."
        )

    if error:
        raise ValueError()

def compile_configs(new_configs):
    error = False
    already_compiled = set()
    for filepath, i, config in new_configs:
        try:
            generated_resources = compile_config(filepath, config)
            for resource in generated_resources:
                key = resource.reference
                if key in already_compiled:
                    print("skipping", key, "since it was already generated")
                    continue
                already_compiled.add(key)
                compiled_resources[key] = resource
        except ValueError as e:
            error = True
        except Exception as e:
            raise e
    
    if error:
        raise ValueError()


def map_config_locations(resources_path, prior_configs):
    error = False

    for path, i, config in prior_configs:
        if not is_itl_resource(config):
            continue
        if config["kind"] == "ResourceDefs":
            continue

        try:
            resource = next(iter(compile_config(path, config)))
        except ValueError as e:
            error = True
            continue

        key = resource.reference
        if key in existing_resources:
            error = True
        else:
            existing_resources[key] = LocatedResource(path, i, resource)
    
    if error:
        raise ValueError()

def link_resources(prior_configs, resources_path, secrets_path):
    error = False
    updated_configs: Dict[str, List[dict]] = defaultdict(list)
    updated_yaml_files = set()

    for filepath, i, config in prior_configs:
        updated_configs[filepath].append(config)

    for reference, target_resource in compiled_resources.items():
        if reference in existing_resources:
            location = existing_resources[reference]
            updated_configs[location.path][location.index] = target_resource.link()
            updated_yaml_files.add(location.path)
        else:
            if reference.kind == "ApiKey":
                target_file = create_resource_path(
                    secrets_path, target_resource.config
                )
            else:
                target_file = create_resource_path(
                    resources_path, target_resource.config
                )

            try:
                updated_configs[target_file].append(target_resource.link())
            except ValueError as e:
                print("Failed to generate config for", reference)
                error = True
                continue
            updated_yaml_files.add(target_file)
    
    if error:
        raise ValueError()

    return {x: updated_configs[x] for x in updated_yaml_files}    


class Commander:
    def apply(self, *files, resources="./resources", secrets="./secrets"):
        """Apply the configuration in the given files."""
        global skipped_apiversion, skipped_kind, existing_resources, compiled_resources

        resources_path = os.path.realpath(resources)
        secrets_path = os.path.realpath(secrets)

        # Collect & sort the configs
        print('=== Reading configs to be applied ===')
        new_configs = sorted(
            list(itertools.chain(*[read_resource_configs_dir(file) for file in files])),
            key=lambda x: config_priority(x[-1]),
        )

        # Print any warnings messages
        if skipped_apiversion:
            print(
                f"To apply configs with skipped apiVersions, change the apiVersion to thatone.ai/v1."
            )

        if skipped_kind:
            print(
                f"To apply configs with skipped kinds, change the kind to one of {', '.join(CONFIG_PRIORITY.keys())}."
            )
        
        # Read the current resources
        print('=== Loading existing configs from', resources_path, 'and', secrets_path, '===')
        prior_configs = list(
            itertools.chain(
                *[read_resource_configs_dir(resources_path, read_fully=True)],
                *[read_resource_configs_dir(secrets_path, read_fully=True)],
            )
        )
        
        try:
            # Check for duplicates in each set separately
            print('=== Checking for duplicates in configs to be applied ===')
            check_duplicates(new_configs)
            print('=== Checking for duplicates in', resources_path, 'and', secrets_path, '===')
            check_duplicates(prior_configs)

            # Find where the resources need to go
            print('=== Checking where new resources should be written ===')
            map_config_locations(resources, prior_configs)

            print('=== Generating new resource files ===')
            # Compile the configs
            compile_configs(new_configs)
            # Link the resources
            updated_configs = link_resources(prior_configs, resources_path, secrets_path)
        except ValueError:
            print("Fix the errors and try again.")
            return

        # Save the yaml files
        for filepath, configs in updated_configs.items():
            dirname = os.path.dirname(filepath)
            os.makedirs(dirname, exist_ok=True)

            with open(filepath, "w") as outp:
                yaml.dump_all(
                    configs,
                    stream=outp,
                    default_flow_style=False,
                    sort_keys=False,
                    explicit_end=False,
                )

        # Push the updates
        for key, resource in compiled_resources.items():
            try:
                print(f'Applying {resource.reference}')
                resource.apply()
            except Exception as e:
                print(f'Failed to apply {resource.reference}')
                print(e)
                raise


if __name__ == "__main__":
    fire.Fire(Commander(), name="itlctl")