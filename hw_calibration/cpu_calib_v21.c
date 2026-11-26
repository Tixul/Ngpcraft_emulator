/* ============================================================================
 * NGPC CALIBRATION ROM (v21, 2026-08-30) -- UN TRANSFERT BLOC, QUATRE CHEMINS
 *
 * LA QUESTION, ET POURQUOI ELLE EST OUVERTE.
 *
 * La v20 a mesure `ldirb` et `ldirw` **RAM -> RAM** : **14,12** et **14,16** cycles par
 * iteration, c'est-a-dire exactement l'annexe B (3), `7n + 1` etats, et exactement la
 * MEME valeur pour les deux formes. Notre `ldirw_cost = 18` est donc faux de 29 % pour ce
 * chemin-la.
 *
 * ⛔ MAIS ON NE PEUT PAS LE CORRIGER : le copieur HiColor de reference
 * exige bien ~18. A 14 il tourne **21 % trop vite** (6476 cycles pour deux blocs contre
 * 8240 mesures sur console) et l'image se dechire -- une fenetre d'UN cycle, verifiee
 * manette en main.
 *
 * ⚡ ET CE COPIEUR NE FAIT PAS LE MEME TRANSFERT QUE LA v20. Il copie **ROM -> VRAM**,
 * la v20 mesurait **RAM -> RAM** : DEUX differences a la fois, la region SOURCE et la
 * region DESTINATION. Tant qu'on ne les separe pas, tout ce qu'on peut faire est de
 * poser un nombre qui arrange un jeu -- ce qui est exactement ce que 18 est aujourd'hui.
 *
 * ⛔ ET L'EXPLICATION EVIDENTE EST DEJA REFUTEE. « 18 = 14 + l'etranglement VRAM » ne
 * tient pas : ce throttle vaut **2,95 cy par acces** (v20 page 3), il en faudrait 4, et
 * arme sur les transferts bloc il ne change RIEN au copieur -- il passe par le
 * recouvrement et s'y fait absorber, le cout de base d'un bloc etant enorme.
 *
 * ===========================================================================
 * LE MONTAGE -- quatre chemins, deux longueurs chacun
 * ===========================================================================
 * Le meme `ldirw`, la meme boucle, seules les regions changent :
 *
 *      RR1 / RR2   RAM -> RAM     (le TEMOIN : la v20 dit 14,16)
 *      RV1 / RV2   RAM -> VRAM    (la destination change, seule)
 *      OR1 / OR2   ROM -> RAM     (la source change, seule)
 *      OV1 / OV2   ROM -> VRAM    (les deux -- le chemin du copieur HiColor)
 *
 * Chaque paire est 64 puis 128 iterations : la difference donne le cout par iteration
 * **sans aucune hypothese** sur le reste (chargement de XHL/XDE/BC identique des deux
 * cotes, il se simplifie).
 *
 * ⇒ Quatre nombres qui se lisent d'un coup :
 *      RV - RR   ce que coute la DESTINATION VRAM
 *      OR - RR   ce que coute la SOURCE ROM (bus 8 bits de la cartouche)
 *      OV - RR   le chemin du copieur HiColor
 *   et surtout : **(OV - RR) est-il la SOMME des deux autres ?** Si oui, les deux effets
 *   sont independants et le modele n'a qu'a les additionner. Sinon il y a un troisieme
 *   terme, et on saura qu'il existe au lieu de le deviner.
 *
 * ⚠️ CE QUI REND LA PAGE HONNETE. Notre modele actuel predit **le meme cout aux quatre
 * chemins** (18,28 partout) : il ne connait ni la source ni la destination. La ROM ne
 * peut donc pas nous confirmer -- elle ne peut que nous corriger, et dire de combien.
 *
 * ⚠️ Destination VRAM = **0xBE00**, comme la v3 : le haut de la RAM de caracteres, que la
 * fonte du BIOS n'utilise pas. 128 iterations mot = 256 octets, soit 0xBE00-0xBEFF : on
 * reste dedans et l'affichage n'est pas touche.
 * ⚠️ Source ROM = **0x200100**, dans notre propre cartouche, en lecture seule.
 *
 * ===========================================================================
 * PAGE 2 -- RASV, la validite du tir. DOIT VALOIR 198.
 * ===========================================================================
 * NAVIGATION : GAUCHE / DROITE. Numero de page en chiffre ligne 1, colonne 12.
 * ⛔ La ROM ne lit la manette QU'UNE FOIS par cycle de mesure : tenir la touche.
 * ⛔ Pas de variable globale (cf. la v6). Ligne-cle "0123456789" en ligne 17.
 *
 * Construit avec la toolchain OFFICIELLE Toshiba cc900. Gear 0 (6,144 MHz).
 * Depouillement : hw_calibration/v21_gate.py
 * ==========================================================================*/

#include "ngpc.h"
#include "carthdr.h"
#include "library.h"

#define RAS_V (*(volatile u8 *)0x8009)
#define FRAMES 60

#define PUSHALL __asm(" push XWA"); __asm(" push BC"); \
                __asm(" push XHL"); __asm(" push XDE");
#define POPALL  __asm(" pop XDE"); __asm(" pop XHL"); \
                __asm(" pop BC"); __asm(" pop XWA");

/* Sources : RAM 0x4800, ROM 0x200100. Destinations : RAM 0x5000, VRAM 0xBE00. */
#define SRC_RAM __asm(" ld XHL,0x004800");
#define SRC_ROM __asm(" ld XHL,0x200100");
#define DST_RAM __asm(" ld XDE,0x005000");
#define DST_VRA __asm(" ld XDE,0x00BE00");

#define CP(S, D, N) S D __asm(" ld BC," #N); __asm(" ldirw (xde+),(xhl+)");

#define RR1 CP(SRC_RAM, DST_RAM, 64)
#define RR2 CP(SRC_RAM, DST_RAM, 128)
#define RV1 CP(SRC_RAM, DST_VRA, 64)
#define RV2 CP(SRC_RAM, DST_VRA, 128)
#define OR1 CP(SRC_ROM, DST_RAM, 64)
#define OR2 CP(SRC_ROM, DST_RAM, 128)
#define OV1 CP(SRC_ROM, DST_VRA, 64)
#define OV2 CP(SRC_ROM, DST_VRA, 128)

#define X4(m)  m m m m
#define X16(m) X4(m) X4(m) X4(m) X4(m)

/* ⛔ 16 unites par lot, et un lot doit rester TRES court devant une trame : la boucle
 * exterieure compte les trames en guettant `RAS_V`, et un lot trop long enjambe une
 * bascule -- le montage compterait faux SANS LE DIRE (piege rencontre sur la v17). */
#define BATCH(NAME, BODY)                                            \
u16 NAME(void)                                                       \
{                                                                    \
    u16 count; u8 frames; u8 prev, cur;                              \
    count = 0; frames = 0; prev = RAS_V;                             \
    while (frames < FRAMES) {                                        \
        PUSHALL                                                      \
        BODY                                                         \
        POPALL                                                       \
        count++;                                                     \
        cur = RAS_V; if (cur < prev) frames++; prev = cur;           \
    }                                                                \
    return count;                                                    \
}

BATCH(m_rr1, X16(RR1))
BATCH(m_rr2, X16(RR2))
BATCH(m_rv1, X16(RV1))
BATCH(m_rv2, X16(RV2))
BATCH(m_or1, X16(OR1))
BATCH(m_or2, X16(OR2))
BATCH(m_ov1, X16(OV1))
BATCH(m_ov2, X16(OV2))

u8 rasv_max(void)
{
    u8 mx; u8 r; u16 s;
    mx = 0;
    for (s = 0; s < 40000; s++) { r = RAS_V; if (r > mx && r < 250) mx = r; }
    return mx;
}

#define PAL 0
#define P   SCR_1_PLANE
#define NPAGES 3

u8 pad_page(u8 page)
{
    u8 j;
    j = JOYPAD;
    if ((j & J_RIGHT) && page < (NPAGES - 1)) page++;
    else if ((j & J_LEFT) && page > 0)        page--;
    return page;
}

void draw_labels(u8 page)
{
    ClearScreen(P);
    PrintString(P, PAL, 1, 17, "0123456789");
    PrintString(P, PAL, 1, 15, "GAUCHE/DROITE=page");

    if (page == 0) {
        PrintString(P, PAL, 1, 1, "BLOC RAM p");
        PrintString(P, PAL, 1, 3, "RR1 ram>ram:");
        PrintString(P, PAL, 1, 4, "RR2 x2     :");
        PrintString(P, PAL, 1, 5, "RV1 ram>vra:");
        PrintString(P, PAL, 1, 6, "RV2 x2     :");
    } else if (page == 1) {
        PrintString(P, PAL, 1, 1, "BLOC ROM p");
        PrintString(P, PAL, 1, 3, "OR1 rom>ram:");
        PrintString(P, PAL, 1, 4, "OR2 x2     :");
        PrintString(P, PAL, 1, 5, "OV1 rom>vra:");
        PrintString(P, PAL, 1, 6, "OV2 x2     :");
    } else {
        PrintString(P, PAL, 1, 1, "VALIDITE p");
        PrintString(P, PAL, 1, 3, "RASV       :");
        PrintString(P, PAL, 1, 5, "doit valoir 198");
        PrintString(P, PAL, 1, 6, "sinon tout est");
        PrintString(P, PAL, 1, 7, "a jeter.");
        PrintString(P, PAL, 1, 9, "note aussi le md5");
    }
    PrintDecimal(P, PAL, 12, 1, (u16)page, 1);
}

void main(void)
{
    u8 page, shown;

    /* ⛔ `CpuSpeed(0)` force le gear 0 (6,144 MHz) : sans lui la ROM mesure une AUTRE
     * horloge que les jeux, et tous les chiffres sont a jeter sans que rien ne le dise. */
    InitNGPC();
    SetBackgroundColour(RGB(2, 2, 4));
    SysSetSystemFont();
    SetPalette(P, PAL, 4, RGB(15, 15, 15), RGB(15, 15, 15), RGB(15, 15, 15));
    CpuSpeed(0);

    page = 0; shown = 255;

    for (;;) {
        if (page != shown) { draw_labels(page); shown = page; }

        if (page == 0) {
            PrintDecimal(P, PAL, 13, 3, m_rr1(), 5);
            PrintDecimal(P, PAL, 13, 4, m_rr2(), 5);
            PrintDecimal(P, PAL, 13, 5, m_rv1(), 5);
            PrintDecimal(P, PAL, 13, 6, m_rv2(), 5);
        } else if (page == 1) {
            PrintDecimal(P, PAL, 13, 3, m_or1(), 5);
            PrintDecimal(P, PAL, 13, 4, m_or2(), 5);
            PrintDecimal(P, PAL, 13, 5, m_ov1(), 5);
            PrintDecimal(P, PAL, 13, 6, m_ov2(), 5);
        } else {
            PrintDecimal(P, PAL, 13, 3, (u16)rasv_max(), 5);
        }
        page = pad_page(page);
    }
}
