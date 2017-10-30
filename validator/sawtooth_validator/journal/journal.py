# Copyright 2016 Intel Corporation
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
# ------------------------------------------------------------------------------

from concurrent.futures import ThreadPoolExecutor
import logging
import queue
import time

from sawtooth_validator.journal.publisher import BlockPublisher
from sawtooth_validator.journal.chain import ChainController
from sawtooth_validator.journal.block_cache import BlockCache
from sawtooth_validator.journal.batch_injector import \
    DefaultBatchInjectorFactory


LOGGER = logging.getLogger(__name__)


class Journal(object):
    """
    Manages the block chain, This responsibility boils down
    1) to evaluating new blocks to determine if they should extend or replace
    the current chain. Handled by the ChainController
    2) Claiming new blocks. Handled by the BlockPublisher.
    This object provides the threading and event queue for the processors.
    """
    def __init__(self,
                 block_store,
                 state_view_factory,
                 block_sender,
                 batch_sender,
                 transaction_executor,
                 squash_handler,
                 identity_signing_key,
                 chain_id_manager,
                 data_dir,
                 config_dir,
                 permission_verifier,
                 check_publish_block_frequency=0.1,
                 block_cache_purge_frequency=30,
                 block_cache_keep_time=300,
                 batch_observers=None,
                 chain_observers=None,
                 block_cache=None,
                 metrics_registry=None):
        """
        Creates a Journal instance.

        Args:
            block_store (:obj:): The block store.
            state_view_factory (:obj:`StateViewFactory`): StateViewFactory for
                read-only state views.
            block_sender (:obj:`BlockSender`): The BlockSender instance.
            batch_sender (:obj:`BatchSender`): The BatchSender instance.
            transaction_executor (:obj:`TransactionExecutor`): A
                TransactionExecutor instance.
            squash_handler (function): Squash handler function for merging
                contexts.
            identity_signing_key (str): Private key for signing blocks
            chain_id_manager (:obj:`ChainIdManager`) The ChainIdManager
                instance.
            data_dir (str): directory for data storage.
            config_dir (str): directory for configuration.
            check_publish_block_frequency(float): delay in seconds between
                checks if a block should be claimed.
            block_cache_purge_frequency (float): delay in seconds between
                purges of the BlockCache.
            block_cache_keep_time (float): time in seconds to hold unaccess
                blocks in the BlockCache.
            chain_observers (list of :obj:`ChainObserver`): Objects to notify
                on chain updates.
            block_cache (:obj:`BlockCache`, optional): A BlockCache to use in
                place of an internally created instance. Defaults to None.
            metrics_registry (:obj:`MetricsRegistry`, optional): Reigstry used
            gather statistics.
        """
        self._block_store = block_store
        self._block_cache = block_cache
        if self._block_cache is None:
            self._block_cache = BlockCache(self._block_store,
                                           block_cache_keep_time,
                                           block_cache_purge_frequency)
        self._state_view_factory = state_view_factory

        self._transaction_executor = transaction_executor
        self._squash_handler = squash_handler
        self._identity_signing_key = identity_signing_key
        self._block_sender = block_sender
        self._batch_sender = batch_sender

        self._block_publisher = None
        self._check_publish_block_frequency = check_publish_block_frequency
        self._batch_queue = queue.Queue()
        self._batch_obs = [] if batch_observers is None else batch_observers

        self._executor_threadpool = ThreadPoolExecutor(1)
        self._chain_controller = None
        self._block_queue = queue.Queue()
        self._chain_id_manager = chain_id_manager
        self._data_dir = data_dir
        self._config_dir = config_dir
        self._permission_verifier = permission_verifier
        self._chain_observers = [] if chain_observers is None \
            else chain_observers

        self._metrics_registry = metrics_registry

    def _init_subprocesses(self):
        batch_injector_factory = DefaultBatchInjectorFactory(
            block_store=self._block_store,
            state_view_factory=self._state_view_factory,
            signing_key=self._identity_signing_key,
        )
        self._block_publisher = BlockPublisher(
            transaction_executor=self._transaction_executor,
            block_cache=self._block_cache,
            state_view_factory=self._state_view_factory,
            block_sender=self._block_sender,
            batch_sender=self._batch_sender,
            batch_queue=self._batch_queue,
            squash_handler=self._squash_handler,
            chain_head=self._block_store.chain_head,
            identity_signing_key=self._identity_signing_key,
            data_dir=self._data_dir,
            config_dir=self._config_dir,
            permission_verifier=self._permission_verifier,
            check_publish_block_frequency=self._check_publish_block_frequency,
            batch_observers=self._batch_obs,
            batch_injector_factory=batch_injector_factory,
            metrics_registry=self._metrics_registry
        )
        self._chain_controller = ChainController(
            block_sender=self._block_sender,
            block_cache=self._block_cache,
            block_queue=self._block_queue,
            state_view_factory=self._state_view_factory,
            executor=self._executor_threadpool,
            transaction_executor=self._transaction_executor,
            chain_head_lock=self._block_publisher.chain_head_lock,
            on_chain_updated=self._block_publisher.on_chain_updated,
            squash_handler=self._squash_handler,
            chain_id_manager=self._chain_id_manager,
            identity_signing_key=self._identity_signing_key,
            data_dir=self._data_dir,
            config_dir=self._config_dir,
            permission_verifier=self._permission_verifier,
            chain_observers=self._chain_observers,
            metrics_registry=self._metrics_registry
        )

    # FXM: this is an inaccurate name.
    def get_current_root(self):
        return self._chain_controller.chain_head.state_root_hash

    def get_block_store(self):
        return self._block_store

    def start(self):
        if self._block_publisher is None and self._chain_controller is None:
            self._init_subprocesses()

        self._block_publisher.start()
        self._chain_controller.start()

    def stop(self):
        # time to murder the child threads. First ask politely for
        # suicide
        self._executor_threadpool.shutdown(wait=True)

        if self._block_publisher is not None:
            self._block_publisher.stop()
            self._block_publisher = None

        if self._chain_controller is not None:
            self._chain_controller.stop()
            self._chain_controller = None

    def on_block_received(self, block):
        """
        New block has been received, queue it with the chain controller
        for processing.
        """
        self._block_queue.put(block)

    def on_batch_received(self, batch):
        """
        New batch has been received, queue it with the BlockPublisher for
        inclusion in the next block.
        """
        self._batch_queue.put(batch)
        for observer in self._batch_obs:
            observer.notify_batch_pending(batch)
