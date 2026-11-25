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

static inline unsigned long long dg_fingerprint(const unsigned char *t, unsigned long long len)
{
    unsigned long long h = 0xcbf29ce484222325ULL, i;   /* FNV-1a 64, 校验和与标志字节当 0 算 */

    for (i = 0; i < len; i++) {
        h ^= (i == DG_CSUM_OFF || i == DG_LOCK_OFF) ? 0 : t[i];
        h *= 0x100000001b3ULL;
    }
    return h;
}

static inline int dg_classify(const unsigned char *t, unsigned long long avail)
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

/* ---- SHA-256 (FIPS 180-4), 给换表前核嵌入的新表用 ---- */
static inline unsigned int dg_ror(unsigned int x, int n) { return (x >> n) | (x << (32 - n)); }

static inline void dg_sha256(const unsigned char *p, unsigned long long len, unsigned char out[32])
{
    static const unsigned int k[64] = {
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};
    unsigned int h[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                         0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    unsigned long long total = len + 9 + ((55 - len) & 63), off, bits = len * 8;
    unsigned int w[64], a, b, c, d, e, f, g, hh, t1, t2;
    int i;

    for (off = 0; off < total; off += 64) {
        for (i = 0; i < 64; i++) {
            unsigned long long pos = off + (unsigned long long)i;
            unsigned char byte = pos < len ? p[pos] : pos == len ? 0x80 :
                                 pos >= total - 8 ? (unsigned char)(bits >> (8 * (total - 1 - pos))) : 0;
            if (i % 4 == 0)
                w[i / 4] = 0;
            w[i / 4] |= (unsigned int)byte << (24 - 8 * (i % 4));
        }
        for (i = 16; i < 64; i++)
            w[i] = w[i - 16] + (dg_ror(w[i - 15], 7) ^ dg_ror(w[i - 15], 18) ^ (w[i - 15] >> 3)) + w[i - 7] +
                   (dg_ror(w[i - 2], 17) ^ dg_ror(w[i - 2], 19) ^ (w[i - 2] >> 10));
        a = h[0]; b = h[1]; c = h[2]; d = h[3]; e = h[4]; f = h[5]; g = h[6]; hh = h[7];
        for (i = 0; i < 64; i++) {
            t1 = hh + (dg_ror(e, 6) ^ dg_ror(e, 11) ^ dg_ror(e, 25)) + ((e & f) ^ (~e & g)) + k[i] + w[i];
            t2 = (dg_ror(a, 2) ^ dg_ror(a, 13) ^ dg_ror(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
            hh = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
        }
        h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
    }
    for (i = 0; i < 32; i++)
        out[i] = (unsigned char)(h[i / 4] >> (24 - 8 * (i % 4)));
}

/* ---- FADT 里的 DSDT 指针 (偏移 40: 32 位 DSDT, 偏移 140: 64 位 X_DSDT, 可能不按 8 字节对齐 -> 按字节读写) ---- */
static inline unsigned long long dg_get(const unsigned char *p, int n)
{
    unsigned long long v = 0;

    while (n--)
        v = v << 8 | p[n];
    return v;
}

static inline void dg_put(unsigned char *p, int n, unsigned long long v)
{
    int i;

    for (i = 0; i < n; i++)
        p[i] = (unsigned char)(v >> (8 * i));
}

static inline int dg_table_ok(const unsigned char *t, const char *sig, unsigned long long minlen)
{
    unsigned long long n, i;
    unsigned int s = 0;

    if (t[0] != sig[0] || t[1] != sig[1] || t[2] != sig[2] || t[3] != sig[3])
        return 0;
    n = dg_get(t + 4, 4);
    if (n < minlen)
        return 0;
    for (i = 0; i < n; i++)
        s += t[i];
    return (s & 0xFF) == 0;
}

/* FADT 指向的 DSDT: X_DSDT 优先; 两个都有又不同 -> 0 (不认) */
static inline unsigned long long dg_fadt_dsdt(const unsigned char *fadt)
{
    unsigned long long d32 = dg_get(fadt + 40, 4), x = dg_get(fadt + 140, 8);

    if (x && d32 && x != d32)
        return 0;
    return x ? x : d32;
}

/* 把 FADT 的 DSDT 指针改到 addr: X_DSDT 原来非 0 就改, 32 位 DSDT 原来非 0 也改 (addr 须 < 4 GiB), 再重算校验和.
 * 返回 0 成功, -1 拒绝 (表不对 / 32 位放不下 / 两个指针原来都是 0). */
static inline int dg_fadt_retarget(unsigned char *fadt, unsigned long long addr)
{
    unsigned long long n, i;
    unsigned int s = 0;
    int did = 0;

    if (!dg_table_ok(fadt, "FACP", 148))
        return -1;
    if (dg_get(fadt + 40, 4)) {
        if (addr > 0xFFFFFFFFULL)
            return -1;
        dg_put(fadt + 40, 4, addr);
        did = 1;
    }
    if (dg_get(fadt + 140, 8)) {
        dg_put(fadt + 140, 8, addr);
        did = 1;
    }
    if (!did)
        return -1;
    n = dg_get(fadt + 4, 4);
    fadt[9] = 0;
    for (i = 0; i < n; i++)
        s += fadt[i];
    fadt[9] = (unsigned char)(0x100 - (s & 0xFF));
    return 0;
}

#endif
