# Copyright 2015, 2016 IBM Corp.
#
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from oslo_log import log as logging
from pypowervm import const as pvm_const
from pypowervm.tasks import partition as pvm_tpar
from pypowervm.tasks import storage as pvm_stg
import six
from taskflow.types import failure as task_fail

from nova_powervm.virt.powervm.i18n import _LE
from nova_powervm.virt.powervm.i18n import _LI
from nova_powervm.virt.powervm.i18n import _LW
from nova_powervm.virt.powervm.tasks import base as pvm_task
from nova_powervm.virt.powervm import vm

from nova.compute import task_states

LOG = logging.getLogger(__name__)


class Get(pvm_task.PowerVMTask):

    """The task for getting a VM entry."""

    def __init__(self, adapter, host_uuid, instance):
        """Creates the Task for getting a VM entry.

        Provides the 'lpar_wrap' for other tasks.

        :param adapter: The adapter for the pypowervm API
        :param host_uuid: The host UUID
        :param instance: The nova instance.
        """
        super(Get, self).__init__(instance, 'get_vm', provides='lpar_wrap')
        self.adapter = adapter
        self.host_uuid = host_uuid

    def execute_impl(self):
        return vm.get_instance_wrapper(self.adapter, self.instance)


class Create(pvm_task.PowerVMTask):

    """The task for creating a VM."""

    def __init__(self, adapter, host_wrapper, instance, flavor, stg_ftsk=None,
                 nvram_mgr=None, slot_mgr=None):
        """Creates the Task for creating a VM.

        The revert method only needs to do something for failed rebuilds.
        Since the rebuild and build methods have different flows, it is
        necessary to clean up the destination LPAR on fails during rebuild.

        The revert method is not implemented for build because the compute
        manager calls the driver destroy operation for spawn errors. By
        not deleting the lpar, it's a cleaner flow through the destroy
        operation and accomplishes the same result.

        Any stale storage associated with the new VM's (possibly recycled) ID
        will be cleaned up.  The cleanup work will be delegated to the FeedTask
        represented by the stg_ftsk parameter.

        Provides the 'lpar_wrap' for other tasks.

        :param adapter: The adapter for the pypowervm API
        :param host_wrapper: The managed system wrapper
        :param instance: The nova instance.
        :param flavor: The nova flavor.
        :param stg_ftsk: (Optional, Default: None) A FeedTask managing storage
                         I/O operations.  If None, one will be built locally
                         and executed immediately. Otherwise it is the caller's
                         responsibility to execute the FeedTask.
        :param nvram_mgr: The NVRAM manager to fetch the NVRAM from. If None,
                          the NVRAM will not be fetched.
        :param slot_mgr: A NovaSlotManager.  Used to store/retrieve the
                         maximum number of virtual slots for the VM.
        """
        super(Create, self).__init__(
            instance, 'crt_vm', provides='lpar_wrap')
        self.adapter = adapter
        self.host_wrapper = host_wrapper
        self.flavor = flavor
        self.stg_ftsk = stg_ftsk or pvm_tpar.build_active_vio_feed_task(
            adapter, name='create_scrubber',
            xag={pvm_const.XAG.VIO_SMAP, pvm_const.XAG.VIO_FMAP})
        self.nvram_mgr = nvram_mgr
        self.slot_mgr = slot_mgr

    def execute_impl(self):
        data = None
        if self.nvram_mgr is not None:
            LOG.info(_LI('Fetching NVRAM for instance %s.'),
                     self.instance.name, instance=self.instance)
            data = self.nvram_mgr.fetch(self.instance)
            LOG.debug('NVRAM data is: %s', data, instance=self.instance)

        wrap = vm.crt_lpar(self.adapter, self.host_wrapper, self.instance,
                           self.flavor, nvram=data, slot_mgr=self.slot_mgr)
        pvm_stg.add_lpar_storage_scrub_tasks([wrap.id], self.stg_ftsk,
                                             lpars_exist=True)
        # If the stg_ftsk passed in was None and we initialized a
        # 'create_scrubber' stg_ftsk then run it immediately. We do
        # this because we moved the LPAR storage scrub tasks out of the
        # build_map initialization. This was so that we could construct the
        # build map earlier in the spawn, just before the LPAR is created.
        # Only rebuilds should be passing in None for stg_ftsk.
        if self.stg_ftsk.name == 'create_scrubber':
            LOG.info(_LI('Scrubbing storage for instance %s as part of '
                         'rebuild.'), self.instance.name,
                     instance=self.instance)
            self.stg_ftsk.execute()

        return wrap

    def revert_impl(self, result, flow_failures, **kwargs):
        # Only reverts failed rebuilds, because the revert
        # for a failed build is handled in the manager.

        if self.instance.task_state == task_states.REBUILD_SPAWNING:
            LOG.info(_LI('Rebuild of instance %s failed. '
                     'Deleting instance from destination.'),
                     self.instance.name, instance=self.instance)
            vm.dlt_lpar(self.adapter, vm.get_pvm_uuid(self.instance))


class Resize(pvm_task.PowerVMTask):

    """The task for resizing an existing VM."""

    def __init__(self, adapter, host_wrapper, instance, flavor, name=None):
        """Creates the Task to resize a VM.

        Provides the 'lpar_wrap' for other tasks.

        :param adapter: The adapter for the pypowervm API
        :param host_wrapper: The managed system wrapper
        :param instance: The nova instance.
        :param flavor: The nova flavor.
        :param name: VM name to use for the update.  Used on resize when we
            want to rename it but not use the instance name.
        """
        super(Resize, self).__init__(
            instance, 'resize_vm', provides='lpar_wrap')
        self.adapter = adapter
        self.host_wrapper = host_wrapper
        self.flavor = flavor
        self.vm_name = name

    def execute_impl(self):
        return vm.update(self.adapter, self.host_wrapper,
                         self.instance, self.flavor, entry=None,
                         name=self.vm_name)


class Rename(pvm_task.PowerVMTask):

    """The task for renaming an existing VM."""

    def __init__(self, adapter, instance, name):
        """Creates the Task to rename a VM.

        Provides the 'lpar_wrap' for other tasks.

        :param adapter: The adapter for the pypowervm API
        :param instance: The nova instance.
        :param name: The new VM name.
        """
        super(Rename, self).__init__(
            instance, 'rename_vm_%s' % name, provides='lpar_wrap')
        self.adapter = adapter
        self.vm_name = name

    def execute_impl(self):
        LOG.info(_LI('Renaming instance to name: %s'), self.name,
                 instance=self.instance)
        return vm.rename(self.adapter, self.instance, self.vm_name)


class PowerOn(pvm_task.PowerVMTask):

    """The task to power on the instance."""

    def __init__(self, adapter, instance, pwr_opts=None):
        """Create the Task for the power on of the LPAR.

        :param adapter: The pypowervm adapter.
        :param instance: The nova instance.
        :param pwr_opts: Additional parameters for the pypowervm PowerOn Job.
        """
        super(PowerOn, self).__init__(instance, 'pwr_vm')
        self.adapter = adapter
        self.instance = instance
        self.pwr_opts = pwr_opts

    def execute_impl(self):
        vm.power_on(self.adapter, self.instance, opts=self.pwr_opts)

    def revert_impl(self, result, flow_failures):
        LOG.warning(_LW('Powering off instance: %s'), self.instance.name)

        if isinstance(result, task_fail.Failure):
            # The power on itself failed...can't power off.
            LOG.debug('Power on failed.  Not performing power off.')
            return

        vm.power_off(self.adapter, self.instance, force_immediate=True)


class PowerOff(pvm_task.PowerVMTask):

    """The task to power off a VM."""

    def __init__(self, adapter, instance, force_immediate=False):
        """Creates the Task to power off an LPAR.

        :param adapter: The adapter for the pypowervm API
        :param lpar_uuid: The UUID of the lpar that has media.
        :param instance: The nova instance.
        :param force_immediate: Boolean. Perform a VSP hard power off.
        """
        super(PowerOff, self).__init__(instance, 'pwr_off_vm')
        self.adapter = adapter
        self.force_immediate = force_immediate

    def execute_impl(self):
        vm.power_off(self.adapter, self.instance,
                     force_immediate=self.force_immediate)


class StoreNvram(pvm_task.PowerVMTask):

    """Store the NVRAM for an instance."""

    def __init__(self, nvram_mgr, instance, immediate=False):
        """Creates a task to store the NVRAM of an instance.

        :param nvram_mgr: The NVRAM manager.
        :param instance: The nova instance.
        :param immediate: boolean whether to update the NVRAM immediately
        """
        super(StoreNvram, self).__init__(instance, 'store_nvram')
        self.nvram_mgr = nvram_mgr
        self.immediate = immediate

    def execute_impl(self):
        if self.nvram_mgr is None:
            return

        try:
            self.nvram_mgr.store(self.instance, immediate=self.immediate)
        except Exception as e:
            LOG.exception(_LE('Unable to store NVRAM for instance '
                              '%(name)s. Exception: %(reason)s'),
                          {'name': self.instance.name,
                           'reason': six.text_type(e)},
                          instance=self.instance)


class DeleteNvram(pvm_task.PowerVMTask):

    """Delete the NVRAM for an instance from the store."""

    def __init__(self, nvram_mgr, instance):
        """Creates a task to delete the NVRAM of an instance.

        :param nvram_mgr: The NVRAM manager.
        :param instance: The nova instance.
        """
        super(DeleteNvram, self).__init__(instance, 'delete_nvram')
        self.nvram_mgr = nvram_mgr

    def execute_impl(self):
        if self.nvram_mgr is None:
            LOG.info(_LI("No op for NVRAM delete."), instance=self.instance)
            return

        LOG.info(_LI('Deleting NVRAM for instance: %s'),
                 self.instance.name, instance=self.instance)
        try:
            self.nvram_mgr.remove(self.instance)
        except Exception as e:
            LOG.exception(_LE('Unable to delete NVRAM for instance '
                              '%(name)s. Exception: %(reason)s'),
                          {'name': self.instance.name,
                           'reason': six.text_type(e)},
                          instance=self.instance)


class Delete(pvm_task.PowerVMTask):

    """The task to delete the instance from the system."""

    def __init__(self, adapter, lpar_uuid, instance):
        """Create the Task to delete the VM from the system.

        :param adapter: The adapter for the pypowervm API.
        :param lpar_uuid: The VM's PowerVM UUID.
        :param instance: The nova instance.
        """
        super(Delete, self).__init__(instance, 'dlt_vm')
        self.adapter = adapter
        self.lpar_uuid = lpar_uuid

    def execute_impl(self):
        vm.dlt_lpar(self.adapter, self.lpar_uuid)


class UpdateIBMiSettings(pvm_task.PowerVMTask):

    """The task to update settings of an ibmi instance."""

    def __init__(self, adapter, instance, boot_type):
        """Create the Task to update settings of the IBMi VM.

        :param adapter: The adapter for the pypowervm API.
        :param instance: The nova instance.
        :param boot_type: The boot type of the instance.
        """
        super(UpdateIBMiSettings, self).__init__(
            instance, 'update_ibmi_settings')
        self.adapter = adapter
        self.boot_type = boot_type

    def execute_impl(self):
        vm.update_ibmi_settings(self.adapter, self.instance, self.boot_type)
