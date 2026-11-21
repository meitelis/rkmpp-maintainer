// Prove RKNN can create a real matmul context and allocate tensor memory.
// Device enumeration alone does not exercise the kernel driver.
#include <cstdio>
#include <cstring>
#include "rknn_matmul_api.h"

int main() {
    rknn_matmul_info info{};
    info.M = 32;
    info.K = 32;
    info.N = 32;
    info.type = RKNN_FLOAT16_MM_FLOAT16_TO_FLOAT32;
    info.iommu_domain_id = 0;
    rknn_matmul_io_attr attrs{};
    rknn_matmul_ctx ctx = 0;
    const int status = rknn_matmul_create(&ctx, &info, &attrs);
    if (status < 0 || !ctx) {
        std::fprintf(stderr, "RKNPU_PROBE_FAILED: matmul context (RKNN %d)\n", status);
        return 2;
    }
    rknn_tensor_mem *memory = rknn_create_mem(ctx, 4096);
    if (!memory) {
        std::fprintf(stderr, "RKNPU_PROBE_FAILED: device tensor allocation\n");
        rknn_matmul_destroy(ctx);
        return 3;
    }
    rknn_destroy_mem(ctx, memory);
    rknn_matmul_destroy(ctx);
    std::puts("RKNPU_PROBE_OK");
    return 0;
}
