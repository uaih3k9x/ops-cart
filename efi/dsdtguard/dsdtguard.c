/* dsdtguard.efi: UEFI Shell 里给 startup.nsh 用的 DSDT 守卫 (只读, 不写任何内存).
 *
 *   dsdtguard.efi orig           这次开机的 DSDT 是认识的原表           -> EFI_SUCCESS, 否则 EFI_ABORTED
 *   dsdtguard.efi patched        是认识的表且已补成 NoLock              -> EFI_SUCCESS, 否则 EFI_ABORTED
 *   dsdtguard.efi selftest-ok    总是 EFI_SUCCESS   } 让脚本先确认 "%lasterror% 能分辨成败" 这件事本身成立,
 *   dsdtguard.efi selftest-fail  总是 EFI_ABORTED   } 再拿它去挡 mm 写内存
 *
 * "认识" = 从 ACPI 2.0 RSDP -> XSDT -> FACP 找到的 DSDT 地址等于 dsdt_expect.h 的 DG_ADDR (startup.nsh 里 mm 写死的地址),
 * 并且 dg_classify (dsdtguard_core.h) 认: 签名、长度、整表字节和、0x3A 前的 Field "GNVS"、其余字节的指纹、两个字节的值.
 * 构建: efi/dsdtguard/build.sh (docker 里 clang + lld-link). 说明: docs/ARM64_WINPE.md 第 7 节. */
#include "dsdt_expect.h"
#include "dsdtguard_core.h"

typedef unsigned long long UINTN;
typedef unsigned long long EFI_STATUS;
typedef unsigned short CHAR16;
typedef void *EFI_HANDLE;

#define EFI_SUCCESS            0ULL
#define EFI_INVALID_PARAMETER  0x8000000000000002ULL
#define EFI_NOT_FOUND          0x800000000000000EULL
#define EFI_ABORTED            0x8000000000000015ULL

typedef struct { unsigned int d1; unsigned short d2, d3; unsigned char d4[8]; } EFI_GUID;
typedef struct { unsigned long long Signature; unsigned int Revision, HeaderSize, CRC32, Reserved; } EFI_TABLE_HEADER;
typedef struct OUT { void *Reset; EFI_STATUS (*OutputString)(struct OUT *, CHAR16 *); } OUT;
typedef struct { EFI_GUID VendorGuid; void *VendorTable; } CONFIG;
typedef struct {
    EFI_TABLE_HEADER Hdr;
    void *RaiseTPL, *RestoreTPL, *AllocatePages, *FreePages, *GetMemoryMap, *AllocatePool, *FreePool,
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

/* 第二个参数 (命令行按空格分, 第一个是程序路径); 认识的返回 1..4, 不认识 0 */
static int mode(LOADED_IMAGE *li)
{
    static CHAR16 *names[] = {L"orig", L"patched", L"selftest-ok", L"selftest-fail"};
    CHAR16 *s = li ? li->LoadOptions : 0;
    unsigned int n = li ? li->LoadOptionsSize / 2 : 0, i = 0, start, k, j;

    if (!s)
        return 0;
    while (i < n && s[i] && s[i] != ' ') i++;          /* 程序路径 */
    while (i < n && s[i] == ' ') i++;
    start = i;
    while (i < n && s[i] && s[i] != ' ') i++;
    for (j = i; j < n && s[j] == ' '; j++) ;
    if (j < n && s[j])                                  /* 多了参数 */
        return 0;
    for (k = 0; k < 4; k++) {
        CHAR16 *w = names[k];
        unsigned int m = 0;
        while (w[m] && start + m < i && s[start + m] == w[m]) m++;
        if (!w[m] && start + m == i)
            return (int)k + 1;
    }
    return 0;
}

static const unsigned char *find_dsdt(SYSTAB *st)
{
    const unsigned char *rsdp = 0, *xsdt, *facp = 0;
    unsigned long long x, d32, i, n;

    for (i = 0; i < st->NumberOfTableEntries; i++)
        if (guid_eq(&st->ConfigurationTable[i].VendorGuid, &ACPI20_GUID))
            rsdp = st->ConfigurationTable[i].VendorTable;
    if (!rsdp || !(rsdp[0] == 'R' && rsdp[1] == 'S' && rsdp[2] == 'D' && rsdp[3] == ' ') || rsdp[15] < 2
            || !sum8(rsdp, 20) || !sum8(rsdp, 36)) {
        say(L"dsdtguard: no valid ACPI 2.0 RSDP\r\n");
        return 0;
    }
    xsdt = (const unsigned char *)(UINTN)u64(rsdp + 24);
    if (!xsdt || !sig(xsdt, "XSDT") || u32(xsdt + 4) < 36 || !sum8(xsdt, u32(xsdt + 4))) {
        say(L"dsdtguard: no valid XSDT\r\n");
        return 0;
    }
    n = (u32(xsdt + 4) - 36) / 8;
    for (i = 0; i < n; i++) {
        const unsigned char *t = (const unsigned char *)(UINTN)u64(xsdt + 36 + 8 * i);
        if (t && sig(t, "FACP"))
            facp = t;
    }
    if (!facp || u32(facp + 4) < 148) {
        say(L"dsdtguard: no FADT (FACP) in XSDT\r\n");
        return 0;
    }
    d32 = u32(facp + 40);
    x = u64(facp + 140);
    if (x && d32 && x != d32) {
        say(L"dsdtguard: FADT DSDT and X_DSDT disagree\r\n");
        return 0;
    }
    if (!x)
        x = d32;
    if (x != DG_ADDR) {
        say(L"dsdtguard: DSDT is at ");
        hex(x);
        say(L", startup.nsh expects ");
        hex(DG_ADDR);
        say(L"\r\n");
        return 0;
    }
    return (const unsigned char *)(UINTN)x;
}

EFI_STATUS efi_main(EFI_HANDLE image, SYSTAB *st)
{
    LOADED_IMAGE *li = 0;
    const unsigned char *t;
    int m, r;

    con = st->ConOut;
    st->BootServices->HandleProtocol(image, &LOADED_IMAGE_GUID, (void **)&li);
    m = mode(li);
    if (m == 3) {
        say(L"dsdtguard: selftest-ok\r\n");
        return EFI_SUCCESS;
    }
    if (m == 4) {
        say(L"dsdtguard: selftest-fail\r\n");
        return EFI_ABORTED;
    }
    if (!m) {
        say(L"usage: dsdtguard.efi orig|patched|selftest-ok|selftest-fail\r\n");
        return EFI_INVALID_PARAMETER;
    }
    t = find_dsdt(st);
    if (!t)
        return EFI_NOT_FOUND;
    r = dg_classify(t, DG_LEN);
    say(L"dsdtguard: DSDT @ ");
    hex(DG_ADDR);
    say(L" lock byte ");
    hexw(t[DG_LOCK_OFF], 2);
    say(L" checksum ");
    hexw(t[DG_CSUM_OFF], 2);
    say(r == DG_ORIG ? L" -> original (Lock)" : r == DG_PATCHED ? L" -> patched (NoLock)" :
        r == DG_E_SIG ? L" -> NOT a DSDT" : r == DG_E_LEN ? L" -> length differs" :
        r == DG_E_SUM ? L" -> checksum bad" : r == DG_E_CTX ? L" -> no Field GNVS at expected offset" :
        r == DG_E_FNV ? L" -> content differs from the known table" : L" -> unknown byte values");
    say(L"\r\n");
    if ((m == 1 && r == DG_ORIG) || (m == 2 && r == DG_PATCHED))
        return EFI_SUCCESS;
    return EFI_ABORTED;
}
