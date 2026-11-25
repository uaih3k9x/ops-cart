/* 主机上跑 efi/dsdtguard/dsdtguard_core.h, 跟 arm64_winpe_local 的 Python 实现对拍 (tests/test_arm64_winpe_local.py).
 *   dsdtguard_host classify <表>          -> dg_classify 的返回值
 *   dsdtguard_host sha256 <文件>          -> 十六进制
 *   dsdtguard_host retarget <FADT> <地址>  -> 改后的 FADT 十六进制, 拒绝时打印 -1 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "dsdt_expect.h"
#include "dsdtguard_core.h"

static unsigned char buf[1 << 20];

int main(int argc, char **argv)
{
    FILE *f;
    size_t n, i;

    if (argc < 3 || !(f = fopen(argv[2], "rb")))
        return 2;
    n = fread(buf, 1, sizeof buf, f);
    fclose(f);
    if (!strcmp(argv[1], "classify")) {
        printf("%d\n", dg_classify(buf, n));
    } else if (!strcmp(argv[1], "sha256")) {
        unsigned char h[32];
        dg_sha256(buf, n, h);
        for (i = 0; i < 32; i++)
            printf("%02x", h[i]);
        printf("\n");
    } else if (!strcmp(argv[1], "retarget") && argc == 4) {
        if (n < 148 || dg_fadt_retarget(buf, strtoull(argv[3], 0, 0))) {
            printf("-1\n");
        } else {
            for (i = 0; i < n; i++)
                printf("%02x", buf[i]);
            printf("\n");
        }
    } else {
        return 2;
    }
    return 0;
}
