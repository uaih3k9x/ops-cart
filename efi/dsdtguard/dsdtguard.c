/* dsdtguard.efi: UEFI Shell 里给 startup.nsh 用的 DSDT 守卫. 只在 swap 时写内存.
 *
 *   dsdtguard.efi orig           这次开机的 DSDT 是认识的原表                 -> EFI_SUCCESS, 否则 EFI_ABORTED
 *   dsdtguard.efi patched        是认识的表且已 mm 补成 NoLock (第 0 轮)        -> 同上
 *   dsdtguard.efi status         打印现状 (原表 / 已换成哪个变体 / 不认识)     -> 认识就 EFI_SUCCESS
 *   dsdtguard.efi swap <变体>    把 DSDT 整表换成内嵌的 <变体> (dsdt_tables.h)  -> 换好或早已换成同一变体 EFI_SUCCESS
 *   dsdtguard.efi selftest-ok    总是 EFI_SUCCESS   } 让脚本先确认 "%lasterror% 能分辨成败" 这件事本身成立,
 *   dsdtguard.efi selftest-fail  总是 EFI_ABORTED   } 再拿它去挡写内存
 *
 * "认识的原表" = 所有 FADT (XSDT 里的, 有 RSDT 也算上) 指的 DSDT 地址都等于 dsdt_expect.h 的 DG_ADDR, 并且 dg_classify 认
 * (签名、长度、整表字节和、0x3A 前的 Field "GNVS"、其余字节的指纹、两个字节的值).
 * swap: 先核内嵌新表 (sha256 写死在 dsdt_tables.h、签名、长度、字节和) -> 核原表 -> AllocatePages (< 4 GiB, EfiACPIReclaimMemory)
 * -> 拷过去再核 sha256 -> 改每个 FADT 的 X_DSDT / 32 位 DSDT 并重算 FADT 校验和 -> 重新走一遍 RSDP 核结果.
 * 所有 FADT 已经指向内嵌的同一变体 = 已换过, 不再申请内存; 指向别的变体或不认识的表 = 拒绝 (断电重来).
 * 为什么不用 EFI_ACPI_TABLE_PROTOCOL.InstallAcpiTable: EDK2 的实现对已有 DSDT 的情况不保证替换 (版本间行为不同),
 * 固件是百敖改的、看不到源码; 直接改 FADT 指针只依赖 ACPI 规范本身, 行为可预期, 也好核.
 * 构建: efi/dsdtguard/build.sh (docker 里 clang + lld-link; 要先有 arm64_dsdt_fix.py build 生成的 dsdt_tables.h).
 * 说明: docs/ARM64_WINPE.md 第 7、8 节. */
#include "dsdt_expect.h"
#include "dsdtguard_core.h"
#include "dsdt_tables.h"

typedef unsigned long long UINTN;
typedef unsigned long long EFI_STATUS;
typedef unsigned short CHAR16;
typedef void *EFI_HANDLE;

#define EFI_SUCCESS            0ULL
#define EFI_INVALID_PARAMETER  0x8000000000000002ULL
#define EFI_NOT_FOUND          0x800000000000000EULL
#define EFI_ABORTED            0x8000000000000015ULL
#define ALLOCATE_MAX_ADDRESS   1
#define EFI_ACPI_RECLAIM       9

typedef struct { unsigned int d1; unsigned short d2, d3; unsigned char d4[8]; } EFI_GUID;
typedef struct { unsigned long long Signature; unsigned int Revision, HeaderSize, CRC32, Reserved; } EFI_TABLE_HEADER;
typedef struct OUT { void *Reset; EFI_STATUS (*OutputString)(struct OUT *, CHAR16 *); } OUT;
typedef struct { EFI_GUID VendorGuid; void *VendorTable; } CONFIG;
typedef struct {
    EFI_TABLE_HEADER Hdr;
    void *RaiseTPL, *RestoreTPL;
    EFI_STATUS (*AllocatePages)(int, int, UINTN, unsigned long long *);
    void *FreePages, *GetMemoryMap, *AllocatePool, *FreePool,
         *CreateEvent, *SetTimer, *WaitForEvent, *SignalEvent, *CloseEvent, *CheckEvent,
         *InstallProtocolInterface, *ReinstallProtocolInterface, *UninstallProtocolInterface;
    EFI_STATUS (*HandleProtocol)(EFI_HANDLE, EFI_GUID *, void **);
} BOOT;
typedef struct {
    EFI_TABLE_HEADER Hdr;
    CHAR16 *FirmwareVendor;
    unsigned int FirmwareRevision;
    EFI_HANDLE ConsoleInHandle; void *ConIn;
    EFI_HANDLE ConsoleOutHandle; OUT *ConOut;
    EFI_HANDLE StandardErrorHandle; OUT *StdErr;
    void *RuntimeServices;
    BOOT *BootServices;
    UINTN NumberOfTableEntries;
    CONFIG *ConfigurationTable;
} SYSTAB;
typedef struct {
    unsigned int Revision;
    EFI_HANDLE ParentHandle; SYSTAB *SystemTable; EFI_HANDLE DeviceHandle; void *FilePath; void *Reserved;
    unsigned int LoadOptionsSize; void *LoadOptions;
} LOADED_IMAGE;

static EFI_GUID ACPI20_GUID = {0x8868e871, 0xe4f1, 0x11d3, {0xbc, 0x22, 0x00, 0x80, 0xc7, 0x3c, 0x88, 0x81}};
static EFI_GUID LOADED_IMAGE_GUID = {0x5b1b31a1, 0x9562, 0x11d2, {0x8e, 0x3f, 0x00, 0xa0, 0xc9, 0x69, 0x72, 0x3b}};
static OUT *con;

/* clang 在 -ffreestanding 下仍可能生成对这两个的调用 */
void *memset(void *d, int c, UINTN n) { unsigned char *p = d; while (n--) *p++ = (unsigned char)c; return d; }
void *memcpy(void *d, const void *s, UINTN n) { unsigned char *p = d; const unsigned char *q = s; while (n--) *p++ = *q++; return d; }

static void say(CHAR16 *s) { if (con) con->OutputString(con, s); }

static void hexw(unsigned long long v, int digits)
{
    CHAR16 b[19];
    int i;

    b[0] = '0'; b[1] = 'x';
    for (i = 0; i < digits; i++)
        b[2 + i] = (CHAR16)"0123456789ABCDEF"[(v >> (4 * (digits - 1 - i))) & 0xF];
    b[2 + digits] = 0;
    say(b);
}

static void hex(unsigned long long v) { hexw(v, 16); }

static int guid_eq(const EFI_GUID *a, const EFI_GUID *b)
{
    const unsigned char *x = (const void *)a, *y = (const void *)b;
    int i;

    for (i = 0; i < 16; i++)
        if (x[i] != y[i])
            return 0;
    return 1;
}

static unsigned int u32(const unsigned char *p) { return p[0] | p[1] << 8 | p[2] << 16 | (unsigned int)p[3] << 24; }
static unsigned long long u64(const unsigned char *p) { return u32(p) | (unsigned long long)u32(p + 4) << 32; }

static int sum8(const unsigned char *p, unsigned int n)
{
    unsigned int s = 0;

    while (n--)
        s += *p++;
    return (s & 0xFF) == 0;
}

static int sig(const unsigned char *p, const char *s) { return p[0] == s[0] && p[1] == s[1] && p[2] == s[2] && p[3] == s[3]; }

/* 命令行 (按空格分, 第一个是程序路径) 的第 idx 个词跟 ASCII 串 w 比; 词的个数 */
static unsigned int nwords(LOADED_IMAGE *li, unsigned int idx, const char *w, int *match)
{
    CHAR16 *s = li ? li->LoadOptions : 0;
    unsigned int n = li ? li->LoadOptionsSize / 2 : 0, i = 0, k = 0, start, m;

    *match = 0;
    while (s && i < n && s[i]) {
        while (i < n && s[i] == ' ') i++;
        if (i >= n || !s[i])
            break;
        start = i;
        while (i < n && s[i] && s[i] != ' ') i++;
        if (k == idx && w) {
            for (m = 0; w[m] && start + m < i && s[start + m] == (CHAR16)w[m]; m++) ;
            *match = !w[m] && start + m == i;
        }
        k++;
    }
    return k;
}

static int arg_is(LOADED_IMAGE *li, unsigned int idx, const char *w)
{
    int m;

    nwords(li, idx, w, &m);
    return m;
}

static const unsigned char *P(unsigned long long a) { return (const unsigned char *)(UINTN)a; }

#define MAX_FADT 4
static unsigned char *fadts[MAX_FADT];
static int nfadt;

static void add_fadt(unsigned long long a)
{
    int i;

    if (!a || !sig(P(a), "FACP"))
        return;
    for (i = 0; i < nfadt; i++)
        if (fadts[i] == (unsigned char *)(UINTN)a)
            return;
    if (nfadt < MAX_FADT)
        fadts[nfadt++] = (unsigned char *)(UINTN)a;
}

/* 从 RSDP 收集所有 FADT (XSDT 的, 有 RSDT 也收), 都要表头完整、校验和对、够长. 返回共同的 DSDT 地址, 不行返回 0. */
static unsigned long long collect(SYSTAB *st)
{
    const unsigned char *rsdp = 0, *t;
    unsigned long long i, n, d = 0, x;

    nfadt = 0;
    for (i = 0; i < st->NumberOfTableEntries; i++)
        if (guid_eq(&st->ConfigurationTable[i].VendorGuid, &ACPI20_GUID))
            rsdp = st->ConfigurationTable[i].VendorTable;
    if (!rsdp || !(rsdp[0] == 'R' && rsdp[1] == 'S' && rsdp[2] == 'D' && rsdp[3] == ' ') || rsdp[15] < 2
            || !sum8(rsdp, 20) || !sum8(rsdp, 36)) {
        say(L"dsdtguard: no valid ACPI 2.0 RSDP\r\n");
        return 0;
    }
    t = P(u64(rsdp + 24));
    if (!t || !dg_table_ok(t, "XSDT", 36)) {
        say(L"dsdtguard: no valid XSDT\r\n");
        return 0;
    }
    n = (u32(t + 4) - 36) / 8;
    for (i = 0; i < n; i++)
        add_fadt(u64(t + 36 + 8 * i));
    t = P(u32(rsdp + 16));
    if (t) {
        if (!dg_table_ok(t, "RSDT", 36)) {
            say(L"dsdtguard: RSDP has an RSDT pointer but the RSDT is invalid\r\n");
            return 0;
        }
        n = (u32(t + 4) - 36) / 4;
        for (i = 0; i < n; i++)
            add_fadt(u32(t + 36 + 4 * i));
    }
    if (!nfadt) {
        say(L"dsdtguard: no FADT (FACP)\r\n");
        return 0;
    }
    for (i = 0; i < (unsigned long long)nfadt; i++) {
        if (!dg_table_ok(fadts[i], "FACP", 148)) {
            say(L"dsdtguard: FADT invalid (length/checksum)\r\n");
            return 0;
        }
        x = dg_fadt_dsdt(fadts[i]);
        if (!x || (d && x != d)) {
            say(L"dsdtguard: FADT DSDT pointers disagree\r\n");
            return 0;
        }
        d = x;
    }
    return d;
}

static int same32(const unsigned char *a, const unsigned char *b)
{
    int i;

    for (i = 0; i < 32; i++)
        if (a[i] != b[i])
            return 0;
    return 1;
}

/* DSDT 地址 d 上的表是哪个内嵌变体: 下标, 不是返回 -1 */
static int which_variant(unsigned long long d)
{
    unsigned char h[32];
    unsigned long long n;
    int i;

    if (!dg_table_ok(P(d), "DSDT", 36))
        return -1;
    n = u32(P(d) + 4);
    dg_sha256(P(d), n, h);
    for (i = 0; i < DG_NTABLES; i++)
        if (dg_tables[i].len == n && same32(h, dg_tables[i].sha256))
            return i;
    return -1;
}

static void say_ascii(const char *a)
{
    CHAR16 b[64];
    int i;

    for (i = 0; a[i] && i < 63; i++)
        b[i] = (CHAR16)a[i];
    b[i] = 0;
    say(b);
}

static int report_orig(unsigned long long d)
{
    const unsigned char *t = P(d);
    int r = dg_classify(t, DG_LEN);

    say(L"dsdtguard: DSDT @ ");
    hex(d);
    say(L" lock byte ");
    hexw(t[DG_LOCK_OFF], 2);
    say(L" checksum ");
    hexw(t[DG_CSUM_OFF], 2);
    say(r == DG_ORIG ? L" -> original (Lock)" : r == DG_PATCHED ? L" -> patched (NoLock)" :
        r == DG_E_SIG ? L" -> NOT a DSDT" : r == DG_E_LEN ? L" -> length differs" :
        r == DG_E_SUM ? L" -> checksum bad" : r == DG_E_CTX ? L" -> no Field GNVS at expected offset" :
        r == DG_E_FNV ? L" -> content differs from the known table" : L" -> unknown byte values");
    say(L"\r\n");
    return r;
}

static EFI_STATUS swap(SYSTAB *st, int v)
{
    const struct dg_table *nt = &dg_tables[v];
    unsigned char h[32];
    unsigned long long d, addr = 0xFFFFFFFFULL, i;
    unsigned char *dst;
    int r, w;

    dg_sha256(nt->aml, nt->len, h);
    if (!same32(h, nt->sha256) || !dg_table_ok(nt->aml, "DSDT", 36) || u32(nt->aml + 4) != nt->len) {
        say(L"dsdtguard: STOP: embedded table failed its sha256/header check\r\n");
        return EFI_ABORTED;
    }
    d = collect(st);
    if (!d)
        return EFI_NOT_FOUND;
    if (d != DG_ADDR) {
        w = which_variant(d);
        say(L"dsdtguard: DSDT @ ");
        hex(d);
        if (w == v) {
            say(L" is already the embedded '");
            say_ascii(nt->name);
            say(L"' table, nothing to do\r\n");
            return EFI_SUCCESS;
        }
        say(w >= 0 ? L" is another embedded variant; power cycle first\r\n" :
                     L" is neither the original nor an embedded table\r\n");
        return EFI_ABORTED;
    }
    r = report_orig(d);
    if (r != DG_ORIG && r != DG_PATCHED)
        return EFI_ABORTED;
    if (st->BootServices->AllocatePages(ALLOCATE_MAX_ADDRESS, EFI_ACPI_RECLAIM, (nt->len + 4095) / 4096, &addr)) {
        say(L"dsdtguard: STOP: AllocatePages failed, nothing changed\r\n");
        return EFI_ABORTED;
    }
    dst = (unsigned char *)(UINTN)addr;
    for (i = 0; i < nt->len; i++)
        dst[i] = nt->aml[i];
    dg_sha256(dst, nt->len, h);
    if (!same32(h, nt->sha256)) {
        say(L"dsdtguard: STOP: copy failed sha256, FADT not changed\r\n");
        return EFI_ABORTED;
    }
    for (i = 0; i < (unsigned long long)nfadt; i++)
        if (dg_fadt_retarget(fadts[i], addr)) {
            say(L"dsdtguard: STOP: FADT update refused (some FADTs may already point to the new table; power cycle)\r\n");
            return EFI_ABORTED;
        }
    if (collect(st) != addr || which_variant(addr) != v) {
        say(L"dsdtguard: STOP: re-check after swap failed (power cycle restores everything)\r\n");
        return EFI_ABORTED;
    }
    say(L"dsdtguard: DSDT replaced by '");
    say_ascii(nt->name);
    say(L"' @ ");
    hex(addr);
    say(L", length ");
    hexw(nt->len, 4);
    say(L", FADTs updated: ");
    hexw((unsigned long long)nfadt, 1);
    say(L"\r\n");
    return EFI_SUCCESS;
}

EFI_STATUS efi_main(EFI_HANDLE image, SYSTAB *st)
{
    LOADED_IMAGE *li = 0;
    unsigned long long d;
    unsigned int words;
    int m, i, r;

    con = st->ConOut;
    st->BootServices->HandleProtocol(image, &LOADED_IMAGE_GUID, (void **)&li);
    words = nwords(li, 0, 0, &m);
    if (words == 2 && arg_is(li, 1, "selftest-ok")) {
        say(L"dsdtguard: selftest-ok\r\n");
        return EFI_SUCCESS;
    }
    if (words == 2 && arg_is(li, 1, "selftest-fail")) {
        say(L"dsdtguard: selftest-fail\r\n");
        return EFI_ABORTED;
    }
    if (words == 3 && arg_is(li, 1, "swap")) {
        for (i = 0; i < DG_NTABLES; i++)
            if (arg_is(li, 2, dg_tables[i].name))
                return swap(st, i);
    } else if (words == 2 && (arg_is(li, 1, "orig") || arg_is(li, 1, "patched") || arg_is(li, 1, "status"))) {
        d = collect(st);
        if (!d)
            return EFI_NOT_FOUND;
        if (d != DG_ADDR) {
            i = which_variant(d);
            say(L"dsdtguard: DSDT @ ");
            hex(d);
            if (i >= 0) {
                say(L" -> swapped to '");
                say_ascii(dg_tables[i].name);
                say(L"'\r\n");
            } else {
                say(L" -> unknown table (original expected at ");
                hex(DG_ADDR);
                say(L")\r\n");
            }
            return arg_is(li, 1, "status") && i >= 0 ? EFI_SUCCESS : EFI_ABORTED;
        }
        r = report_orig(d);
        if (arg_is(li, 1, "status"))
            return r == DG_ORIG || r == DG_PATCHED ? EFI_SUCCESS : EFI_ABORTED;
        return (arg_is(li, 1, "orig") && r == DG_ORIG) || (arg_is(li, 1, "patched") && r == DG_PATCHED)
               ? EFI_SUCCESS : EFI_ABORTED;
    }
    say(L"usage: dsdtguard.efi orig|patched|status|selftest-ok|selftest-fail|swap <");
    for (i = 0; i < DG_NTABLES; i++) {
        if (i)
            say(L"|");
        say_ascii(dg_tables[i].name);
    }
    say(L">\r\n");
    return EFI_INVALID_PARAMETER;
}
