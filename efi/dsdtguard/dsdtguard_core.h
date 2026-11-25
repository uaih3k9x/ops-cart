/* DSDT 守卫的判定 (纯函数, 不依赖 UEFI). dsdtguard.c 和主机测试 tests/dsdtguard_host.c 共用;
 * 跟 arm64_winpe_local.classify 一一对应 (返回码相同, 测试对拍). 先 #include "dsdt_expect.h". */
#ifndef DSDTGUARD_CORE_H
#define DSDTGUARD_CORE_H

#define DG_ORIG     1      /* 认识的原表: Field (GNVS, ..., Lock) */
#define DG_PATCHED  2      /* 认识的表, 已经补成 NoLock、校验和也改好 */
#define DG_E_SIG   -1      /* 不是 DSDT */
#define DG_E_LEN   -2      /* 长度跟预期不同 (或读到的不够) */
#define DG_E_SUM   -3      /* 整表字节和不为 0 */
#define DG_E_CTX   -4      /* 0x3A 前面不是 Field "GNVS": 那个字节不是我们以为的标志字节 */
#define DG_E_FNV   -5      /* 除了要改的两个字节以外, 内容跟预期不同 (别的固件 / 别的板子) */
#define DG_E_STATE -6      /* 两个字节既不是原值也不是补丁值 (半补 / 别人改过) */

static unsigned long long dg_fingerprint(const unsigned char *t, unsigned long long len)
{
    unsigned long long h = 0xcbf29ce484222325ULL, i;   /* FNV-1a 64, 校验和与标志字节当 0 算 */

    for (i = 0; i < len; i++) {
        h ^= (i == DG_CSUM_OFF || i == DG_LOCK_OFF) ? 0 : t[i];
        h *= 0x100000001b3ULL;
    }
    return h;
}

static int dg_classify(const unsigned char *t, unsigned long long avail)
{
    static const unsigned char ctx[] = DG_CTX;
    unsigned long long len, i;
    unsigned int sum = 0;

    if (avail < 36)
        return DG_E_LEN;
    if (t[0] != 'D' || t[1] != 'S' || t[2] != 'D' || t[3] != 'T')
        return DG_E_SIG;
    len = (unsigned long long)t[4] | (unsigned long long)t[5] << 8 |
          (unsigned long long)t[6] << 16 | (unsigned long long)t[7] << 24;
    if (len != DG_LEN || avail < len)
        return DG_E_LEN;
    for (i = 0; i < len; i++)
        sum += t[i];
    if (sum & 0xFF)
        return DG_E_SUM;
    for (i = 0; i < sizeof ctx; i++)
        if (t[DG_CTX_OFF + i] != ctx[i])
            return DG_E_CTX;
    if (dg_fingerprint(t, len) != DG_FNV)
        return DG_E_FNV;
    if (t[DG_LOCK_OFF] == DG_LOCK_OLD && t[DG_CSUM_OFF] == DG_CSUM_OLD)
        return DG_ORIG;
    if (t[DG_LOCK_OFF] == DG_LOCK_NEW && t[DG_CSUM_OFF] == DG_CSUM_NEW)
        return DG_PATCHED;
    return DG_E_STATE;
}

#endif
