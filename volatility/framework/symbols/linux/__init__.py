# This file is Copyright 2019 Volatility Foundation and licensed under the Volatility Software License 1.0
# which is available at https://www.volatilityfoundation.org/license/vsl-v1.0
#
from volatility.framework import exceptions, constants, interfaces, objects
from volatility.framework.symbols import intermed
from volatility.framework.symbols.linux import extensions


class LinuxKernelIntermedSymbols(intermed.IntermediateSymbolTable):
    provides = {"type": "interface"}

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        # Set-up Linux specific types
        self.set_type_class('file', extensions.struct_file)
        self.set_type_class('list_head', extensions.list_head)
        self.set_type_class('mm_struct', extensions.mm_struct)
        self.set_type_class('super_block', extensions.super_block)
        self.set_type_class('task_struct', extensions.task_struct)
        self.set_type_class('vm_area_struct', extensions.vm_area_struct)
        self.set_type_class('qstr', extensions.qstr)
        self.set_type_class('dentry', extensions.dentry)
        self.set_type_class('fs_struct', extensions.fs_struct)
        self.set_type_class('files_struct', extensions.files_struct)
        self.set_type_class('vfsmount', extensions.vfsmount)
        self.set_type_class('kobject', extensions.kobject)

        if 'module' in self.types:
            self.set_type_class('module', extensions.module)

        if 'mount' in self.types:
            self.set_type_class('mount', extensions.mount)


class LinuxUtilities(object):
    """Class with multiple useful linux functions."""

    # based on __d_path from the Linux kernel
    @classmethod
    def _do_get_path(cls, rdentry, rmnt, dentry, vfsmnt) -> str:

        ret_path = []  # type: List[str]

        while dentry != rdentry or vfsmnt != rmnt:
            dname = dentry.path()
            if dname == "":
                break

            ret_path.insert(0, dname.strip('/'))
            if dentry == vfsmnt.get_mnt_root() or dentry == dentry.d_parent:
                if vfsmnt.get_mnt_parent() == vfsmnt:
                    break

                dentry = vfsmnt.get_mnt_mountpoint()
                vfsmnt = vfsmnt.get_mnt_parent()

                continue

            parent = dentry.d_parent
            dentry = parent

        # if we did not gather any valid dentrys in the path, then the entire file is
        # either 1) smeared out of memory or 2) de-allocated and corresponding structures overwritten
        # we return an empty string in this case to avoid confusion with something like a handle to the root
        # directory (e.g., "/")
        if not ret_path:
            return ""

        ret_val = '/'.join([str(p) for p in ret_path if p != ""])

        if ret_val.startswith(("socket:", "pipe:")):
            if ret_val.find("]") == -1:
                try:
                    inode = dentry.d_inode
                    ino = inode.i_ino
                except exceptions.InvalidAddressException:
                    ino = 0

                ret_val = ret_val[:-1] + ":[{0}]".format(ino)
            else:
                ret_val = ret_val.replace("/", "")

        elif ret_val != "inotify":
            ret_val = '/' + ret_val

        return ret_val

    # method used by 'older' kernels
    # TODO: lookup when dentry_operations->d_name was merged into the mainline kernel for exact version
    @classmethod
    def _get_path_file(cls, task, filp) -> str:
        rdentry = task.fs.get_root_dentry()
        rmnt = task.fs.get_root_mnt()
        dentry = filp.get_dentry()
        vfsmnt = filp.get_vfsmnt()

        return LinuxUtilities._do_get_path(rdentry, rmnt, dentry, vfsmnt)

    @classmethod
    def _get_new_sock_pipe_path(cls, context, task, filp) -> str:
        dentry = filp.get_dentry()

        sym_addr = dentry.d_op.d_dname

        symbol_table_arr = sym_addr.vol.type_name.split("!")
        symbol_table = None
        if len(symbol_table_arr) == 2:
            symbol_table = symbol_table_arr[0]

        symbs = list(context.symbol_space.get_symbols_by_location(sym_addr, table_name = symbol_table))

        if len(symbs) == 1:
            sym = symbs[0].split(constants.BANG)[1]

            if sym == "sockfs_dname":
                pre_name = "socket"

            elif sym == "anon_inodefs_dname":
                pre_name = "anon_inode"

            elif sym == "pipefs_dname":
                pre_name = "pipe"

            elif sym == "simple_dname":
                pre_name = cls._get_path_file(task, filp)

            else:
                pre_name = "<unsupported d_op symbol: {0}>".format(sym)

            ret = "{0}:[{1:d}]".format(pre_name, dentry.d_inode.i_ino)

        else:
            ret = "<invalid d_dname pointer> {0:x}".format(sym_addr)

        return ret

    # a 'file' structure doesn't have enough information to properly restore its full path
    # we need the root mount information from task_struct to determine this
    @classmethod
    def path_for_file(cls, context, task, filp) -> str:
        try:
            dentry = filp.get_dentry()
        except exceptions.InvalidAddressException:
            return ""

        if dentry == 0:
            return ""

        dname_is_valid = False

        # TODO COMPARE THIS IN LSOF OUTPUT TO VOL2
        try:
            if dentry.d_op and dentry.d_op.has_member("d_dname") and dentry.d_op.d_dname:
                dname_is_valid = True

        except exceptions.InvalidAddressException:
            dname_is_valid = False

        if dname_is_valid:
            ret = LinuxUtilities._get_new_sock_pipe_path(context, task, filp)
        else:
            ret = LinuxUtilities._get_path_file(task, filp)

        return ret

    @classmethod
    def files_descriptors_for_process(cls, context: interfaces.context.ContextInterface, symbol_table: str,
                                      task: interfaces.objects.ObjectInterface):

        fd_table = task.files.get_fds()
        if fd_table == 0:
            return

        max_fds = task.files.get_max_fds()

        # corruption check
        if max_fds > 500000:
            return

        file_type = symbol_table + constants.BANG + 'file'

        fds = objects.utility.array_of_pointers(fd_table, count = max_fds, subtype = file_type, context = context)

        for (fd_num, filp) in enumerate(fds):
            if filp != 0:
                full_path = LinuxUtilities.path_for_file(context, task, filp)

                yield fd_num, filp, full_path
