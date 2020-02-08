import base64
import collections
import importlib

import grpc
import snet.sdk.generic_client_interceptor as generic_client_interceptor
import web3
from eth_account.messages import defunct_hash_message
from rfc3986 import urlparse
from snet.sdk.mpe.payment_channel_provider import PaymentChannelProvider
from snet.snet_cli.utils import RESOURCES_PATH, add_to_path


class _ClientCallDetails(
        collections.namedtuple(
            '_ClientCallDetails',
            ('method', 'timeout', 'metadata', 'credentials')),
        grpc.ClientCallDetails):
    pass


#Todo remove direct usage of sdk_context from service client.
#todo fix mpe_contract dependency , it is getting created and being used at multiple place
#TODO Law of Demeter not being followed properly

class ServiceClient:
    def __init__(self, sdk, service_metadata,group, service_stub, payment_strategy,
                 options):
        self.sdk = sdk
        self.options = options
        self.group = group
        self.service_metadata = service_metadata

        self.payment_strategy = payment_strategy
        self.expiry_threshold = self.group["payment"]["payment_expiration_threshold"]
        self._base_grpc_channel = self._get_grpc_channel()
        self.grpc_channel = grpc.intercept_channel(self._base_grpc_channel,
                                                   generic_client_interceptor.create(self._intercept_call))
        self.payment_channel_provider=PaymentChannelProvider(sdk.web3,self._generate_payment_channel_state_service_client(),self.sdk.mpe_contract.contract.address)
        self.service = self._generate_grpc_stub(service_stub)
        self.payment_channels = []
        self.last_read_block = 0


    def _get_payment_expiration_threshold_for_group(self):
        pass


    def _generate_grpc_stub(self, service_stub):
        grpc_channel = self._base_grpc_channel
        disable_blockchain_operations = self.options.get("disable_blockchain_operations", False)
        if disable_blockchain_operations is False:
            grpc_channel = self.grpc_channel
        stub_instance = service_stub(grpc_channel)
        return stub_instance


    def _get_grpc_channel(self):
        endpoint = self.options.get("endpoint", None)
        if endpoint is None:
            endpoint = self.service_metadata.get_all_endpoints_for_group(self.group["group_name"])[0]
        endpoint_object = urlparse(endpoint)
        if endpoint_object.port is not None:
            channel_endpoint = endpoint_object.hostname + ":" + str(endpoint_object.port)
        else:
            channel_endpoint = endpoint_object.hostname

        if endpoint_object.scheme == "http":
            return grpc.insecure_channel(channel_endpoint)
        elif endpoint_object.scheme == "https":
            return grpc.secure_channel(channel_endpoint, grpc.ssl_channel_credentials())
        else:
            raise ValueError('Unsupported scheme in service metadata ("{}")'.format(endpoint_object.scheme))



    def _get_service_call_metadata(self):
        metadata = self.payment_strategy.get_payment_metadata(self)
        return metadata


    def _intercept_call(self, client_call_details, request_iterator, request_streaming,
                       response_streaming):
        metadata = []
        if client_call_details.metadata is not None:
            metadata = list(client_call_details.metadata)
        metadata.extend(self._get_service_call_metadata())
        client_call_details = _ClientCallDetails(
            client_call_details.method, client_call_details.timeout, metadata,
            client_call_details.credentials)
        return client_call_details, request_iterator, None

    def _filter_existing_channels_from_new_payment_channels(self, new_payment_channels):
        new_channels_to_be_added = []

        # need to change this logic ,use maps to manage channels so that we can easily navigate it
        for new_payment_channel in new_payment_channels:
            existing_channel = False
            for existing_payment_channel in self.payment_channels:
                if new_payment_channel.channel_id == existing_payment_channel.channel_id:
                    existing_channel = True
                    break

            if not existing_channel:
                new_channels_to_be_added.append(new_payment_channel)


        return new_channels_to_be_added

    def load_open_channels(self):
        current_block_number = self.sdk.web3.eth.getBlock("latest").number
        payment_addrss=self.group["payment"]["payment_address"]
        group_id= base64.b64decode(str(self.group["group_id"]))
        new_payment_channels = self.payment_channel_provider.get_past_open_channels(self.sdk.account, payment_addrss,group_id, self.last_read_block)
        self.payment_channels = self.payment_channels + self._filter_existing_channels_from_new_payment_channels(new_payment_channels)
        self.last_read_block = current_block_number
        return self.payment_channels


    def update_channel_states(self):
        for channel in self.payment_channels:
            channel.sync_state()
        return self.payment_channels


    def default_channel_expiration(self):
        current_block_number = self.sdk.web3.eth.getBlock("latest").number
        return current_block_number + self.expiry_threshold


    def _generate_payment_channel_state_service_client(self):
        grpc_channel = self._base_grpc_channel
        with add_to_path(str(RESOURCES_PATH.joinpath("proto"))):
            state_service_pb2_grpc = importlib.import_module("state_service_pb2_grpc")
        return state_service_pb2_grpc.PaymentChannelStateServiceStub(grpc_channel)

    def open_channel(self, amount, expiration):
        payment_address = self.group["payment"]["payment_address"]
        group_id = base64.b64decode(str(self.group["group_id"]))
        return self.payment_channel_provider.open_channel(self.sdk.account, amount, expiration, payment_address,
                                                          group_id)

    def deposit_and_open_channel(self, amount, expiration):
        payment_address = self.group["payment"]["payment_address"]
        group_id = base64.b64decode(str(self.group["group_id"]))
        return self.payment_channel_provider.deposit_and_open_channel(self.sdk.account, amount, expiration,
                                                                      payment_address, group_id)
