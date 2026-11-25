/* 主机上跑 efi/dsdtguard/dsdtguard_core.h 的判定, 跟 arm64_winpe_local.classify 对拍 (tests/test_arm64_winpe_local.py).
 * 用法: dsdtguard_host <表文件>  -> 打印 dg_classify 的返回值 */
#include <stdio.h>
#include "dsdt_expect.h"
#include "dsdtguard_core.h"

int main(int argc, char **argv)
{
    static unsigned char buf[1 << 20];
    FILE *f;
    size_t n;

    if (argc != 2 || !(f = fopen(argv[1], "rb")))
        return 2;
    n = fread(buf, 1, sizeof buf, f);
    fclose(f);
    printf("%d\n", dg_classify(buf, n));
    return 0;
}
