"""Actual GPU vector addition via CUDA Driver API, without downloaded Python packages."""
import argparse
import ctypes as C
import json
from pathlib import Path

PTX = b'''
.version 6.0
.target sm_52
.address_size 64
.visible .entry add_vectors(
    .param .u64 a, .param .u64 b, .param .u64 out, .param .u32 count
) {
    .reg .pred p;
    .reg .b32 r<5>;
    .reg .b64 rd<8>;
    .reg .f32 f<4>;
    ld.param.u64 rd1, [a];
    ld.param.u64 rd2, [b];
    ld.param.u64 rd3, [out];
    ld.param.u32 r1, [count];
    mov.u32 r2, %tid.x;
    mov.u32 r3, %ctaid.x;
    mov.u32 r4, %ntid.x;
    mad.lo.u32 r2, r3, r4, r2;
    setp.ge.u32 p, r2, r1;
    @p bra done;
    mul.wide.u32 rd4, r2, 4;
    add.u64 rd5, rd1, rd4;
    add.u64 rd6, rd2, rd4;
    add.u64 rd7, rd3, rd4;
    ld.global.f32 f1, [rd5];
    ld.global.f32 f2, [rd6];
    add.f32 f3, f1, f2;
    st.global.f32 [rd7], f3;
done:
    ret;
}
'''


def gpu_add(a, b):
    if len(a) != len(b) or not a:
        raise ValueError('non-empty vectors of equal length required')
    lib = C.CDLL('libcuda.so.1')
    def bind(name, types):
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = types, C.c_int
        def call(*args):
            code = fn(*args)
            if code:
                raise RuntimeError('%s failed with CUDA error %s' % (name, code))
        return call
    ptr, deviceptr = C.c_void_p, C.c_uint64
    init = bind('cuInit', [C.c_uint])
    get_device = bind('cuDeviceGet', [C.POINTER(C.c_int), C.c_int])
    get_name = bind('cuDeviceGetName', [ptr, C.c_int, C.c_int])
    create = bind('cuCtxCreate_v2', [C.POINTER(ptr), C.c_uint, C.c_int])
    destroy = bind('cuCtxDestroy_v2', [ptr])
    load = bind('cuModuleLoadData', [C.POINTER(ptr), ptr])
    unload = bind('cuModuleUnload', [ptr])
    function = bind('cuModuleGetFunction', [C.POINTER(ptr), ptr, C.c_char_p])
    allocate = bind('cuMemAlloc_v2', [C.POINTER(deviceptr), C.c_size_t])
    free = bind('cuMemFree_v2', [deviceptr])
    to_gpu = bind('cuMemcpyHtoD_v2', [deviceptr, ptr, C.c_size_t])
    to_cpu = bind('cuMemcpyDtoH_v2', [ptr, deviceptr, C.c_size_t])
    launch = bind('cuLaunchKernel', [ptr, C.c_uint, C.c_uint, C.c_uint,
                                   C.c_uint, C.c_uint, C.c_uint, C.c_uint, ptr, ptr, ptr])
    synchronize = bind('cuCtxSynchronize', [])
    init(0)
    device, context, module, kernel = C.c_int(), ptr(), ptr(), ptr()
    name = C.create_string_buffer(256)
    get_device(C.byref(device), 0)
    get_name(name, len(name), device)
    create(C.byref(context), 0, device)
    buffers = []
    try:
        load(C.byref(module), C.c_char_p(PTX))
        function(C.byref(kernel), module, b'add_vectors')
        n = len(a)
        vector = C.c_float * n
        host_a, host_b, host_out = vector(*a), vector(*b), vector()
        for _ in range(3):
            buf = deviceptr()
            allocate(C.byref(buf), C.sizeof(host_out))
            buffers.append(buf)
        to_gpu(buffers[0], host_a, C.sizeof(host_a))
        to_gpu(buffers[1], host_b, C.sizeof(host_b))
        count = C.c_uint(n)
        params = (ptr * 4)(*[C.cast(C.byref(x), ptr) for x in buffers + [count]])
        launch(kernel, (n + 127) // 128, 1, 1, 128, 1, 1, 0, None, params, None)
        synchronize()
        to_cpu(host_out, buffers[2], C.sizeof(host_out))
        return {'backend': 'cuda', 'device': name.value.decode(), 'values': list(host_out)}
    finally:
        for buf in buffers:
            free(buf)
        if module:
            unload(module)
        destroy(context)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    data = json.loads(args.input.read_text())
    result = gpu_add(data['a'], data['b'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True) + '\n')
    print(json.dumps(result, sort_keys=True))
