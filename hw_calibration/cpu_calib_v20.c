/* ============================================================================
 * NGPC CALIBRATION ROM (v20, 2026-08-30) -- LE TOUR DES MESURES MANQUANTES
 *
 * POURQUOI UNE SEULE ROM POUR QUATRE QUESTIONS.
 *
 * Apres la campagne v13-v19 le modele de temps est a 0,32 % sur 26 cases silicium et le
 * chemin d'une interruption tombe sur l'annexe B au cycle pres. Ce qui reste n'est plus
 * un ecart diffus : c'est une LISTE de quantites qui n'ont jamais ete mesurees sur
 * console, chacune livree sur une base plus faible que le reste du modele. Un seul tir
 * peut les fermer toutes, donc une seule ROM.
 *
 * ⚖️ CE QUE LA DATASHEET A DEJA REGLE, ET QUI N'EST DONC PAS ICI :
 *   - `ldir_cost = 14` : annexe B (3) donne `LDIR<W>` a **7n + 1 ETATS**, soit
 *     14 cy/iteration. Notre 14 EST la datasheet -- le commentaire du coeur le
 *     presentait comme un ecart, il n'en est pas un.
 *   - le terme constant : `+1` etait en CYCLES la ou la table dit **+1 ETAT** = +2.
 *   - l'entree en interruption : 18 etats, table (11), `JP (vecteur)` compris.
 *   - le mode d'adressage : table (10), `(R)` = +0, `(#16)` = +2.
 *
 * ⛔ CE QUE LA DATASHEET NE PEUT PAS REGLER, ET QUI EST DONC ICI : les quatre pages.
 *
 * ===========================================================================
 * PAGE 0 -- LDIR OCTET CONTRE LDIR MOT   (B1 B2 W1 W2)
 * ===========================================================================
 * ⚡ LA DATASHEET ET NOTRE MESURE SE CONTREDISENT. L'annexe B (3) donne le MEME `7n + 1`
 * pour la forme octet et pour la forme mot -- donc 14 cy par iteration dans les deux cas.
 * Nous livrons `ldir_cost = 14` (d'accord) mais `ldirw_cost = 18`, cale sur le copieur
 * HiColor de reference, un oracle MAISON, jamais sur silicium. 18 contre 14, c'est 29 %.
 *
 * Le montage : le meme lot, a deux longueurs, dans chaque forme.
 *      B1 / B2   16 x `ldirb` de 64 / 128 iterations
 *      W1 / W2   16 x `ldirw` de 64 / 128 iterations
 * La difference B2-B1 donne le cout d'une iteration OCTET sans aucune hypothese sur le
 * reste (le chargement de XHL/XDE/BC est identique des deux cotes et se simplifie).
 * W2-W1 fait de meme pour le MOT. ⇒ **deux nombres, tires de quatre.**
 *
 * ===========================================================================
 * PAGE 1 -- LA FILE APRES UN TRANSFERT BLOC   (Q0 Q4 R4 R8)
 * ===========================================================================
 * `block_drains_queue` est ARME dans le modele livre -- un transfert bloc tient le bus,
 * donc rien n'a ete prefetche derriere lui et l'instruction suivante paye son fetch plein
 * tarif. C'est plausible, et c'est mesure sur le MEME oracle maison. Jamais sur console.
 *      Q0   64 x `ldirb` de 2 iterations
 *      Q4   64 x (`ldirb` 2 + 4 charges `ld XWA,#imm32`)
 *      R4   64 x (4 charges), sans aucun bloc
 *      R8   64 x (8 charges), sans aucun bloc
 * ⛔ LES BLOCS SONT COURTS EXPRES. Le vidage coute AU PLUS un remplissage de file
 * (~16 cy) par transfert, quelle que soit sa longueur : noye dans une copie de 64
 * iterations (900 cy) il rend 0,4 cy, sous la resolution des compteurs. A 2 iterations
 * (30 cy) il pese autant que la copie.
 * ⛔ IL FAUT LES DEUX, R4 ET R8. Une premiere version comparait (Q4-Q0), une DIFFERENCE
 * propre, au NIVEAU de R4 -- lequel contient aussi le `push`/`pop` du lot et la boucle
 * exterieure. On aurait compare une difference a un niveau, et le montage aurait rendu
 * un ecart NEGATIF (une charge « moins chere » apres un bloc) sans que ce soit vrai.
 * (R8 - R4) est la difference propre correspondante : 64 charges de plus, sans bloc.
 * ⇒ **(Q4-Q0) == (R8-R4) : la file n'est PAS videe. (Q4-Q0) plus cher : elle l'est**,
 * et l'ecart donne de combien.
 *
 * ===========================================================================
 * PAGE 2 -- LA DIVISION EST-ELLE A LATENCE VARIABLE ?   (D0 D1 D2)
 * ===========================================================================
 * Les deux pires cases du corpus sont `DIV` (+1,9 % et +1,5 %), et trois autorites
 * donnent trois nombres : annexe B **23 etats**, ROM v17 (pente marginale) **52 cy**,
 * corpus (niveau d'une boucle) **56**. L'hypothese posee pour expliquer ca est que la
 * division est a **latence variable** et que la table donne un plancher -- mais elle n'a
 * jamais ete testee. Ces trois rotations ne different QUE par les operandes :
 *      D0   XWA = 0x00000007  DE = 3        (petit / petit)
 *      D1   XWA = 0x0000FFFF  DE = 3        (grand quotient)
 *      D2   XWA = 0x3FFF0000  DE = 0x7FFF   (grand / grand)
 * ⚠️ Les trois quotients tiennent dans 16 bits : aucun debordement, donc aucune sortie
 * anticipee qui fausserait la comparaison.
 * ⇒ **Trois nombres egaux = latence FIXE**, et alors une constante unique existe et il
 * faut chercher ailleurs l'ecart du corpus. **Trois nombres differents = latence
 * variable**, et alors aucune constante ne peut etre juste : on le saura pour de bon.
 *
 * ===========================================================================
 * PAGE 3 -- L'ETRANGLEMENT VRAM : PAR ACCES OU PAR OCTET ?   (V8B V8W R8B R8W)
 * ===========================================================================
 * `vram_wait = 9` vient d'etre epingle (v3 : VWR 452), mais il est facture **par octet**
 * sans que rien ne le dise -- et c'est exactement la question que la v15 a tranchee pour
 * les acces de donnee ordinaires, ou la reponse etait **par ACCES**. Une forme fausse se
 * paie cher : la v15 a refute une premiere version par octet en une ligne.
 *      V8B / V8W   16 x 8 ecritures OCTET / MOT en VRAM (0xBE00)
 *      R8B / R8W   16 x 8 ecritures OCTET / MOT en RAM  (0x4200) -- le TEMOIN
 * ⛔ DOUBLE DIFFERENCE, ET ELLE EST INDISPENSABLE. Une ecriture MOT et une ecriture
 * OCTET n'ont ni le meme encodage ni le meme nombre d'etats : comparer V8W a V8B
 * melangerait cette difference-la avec l'etranglement. Les memes instructions refaites
 * en RAM, ou il n'y a pas de throttle, l'eliminent :
 *      throttle_octet = V8B - R8B        throttle_mot = V8W - R8W
 * ⇒ **par ACCES : les deux ecarts sont EGAUX. Par OCTET : celui du MOT est le DOUBLE.**
 * Les deux hypotheses donnent des reponses differentes : la page ne peut pas dire
 * « peut-etre ».
 *
 * ⚠️ Les ecritures vont en 0xBE00, comme la v3 -- hors de la zone que l'ecran affiche
 * pendant la mesure, donc rien ne clignote et la mesure ne perturbe pas l'affichage.
 *
 * ===========================================================================
 * PAGE 4 -- RASV, la validite du tir. DOIT VALOIR 198.
 * ===========================================================================
 * NAVIGATION : GAUCHE / DROITE. Numero de page en chiffre ligne 1.
 * ⛔ Pas de variable globale (cf. la v6). Ligne-cle "0123456789" en ligne 17.
 * ⛔ Chaque lot doit rester TRES court devant une trame : la boucle exterieure compte les
 * trames en guettant `RAS_V`, et un lot trop long enjambe une bascule -- le montage
 * compterait faux SANS LE DIRE (piege rencontre sur la v17).
 *
 * Construit avec la toolchain OFFICIELLE Toshiba cc900. Gear 0 (6,144 MHz).
 * Depouillement : hw_calibration/v20_gate.py
 * ==========================================================================*/

#include "ngpc.h"
#include "carthdr.h"
#include "library.h"

#define RAS_V (*(volatile u8 *)0x8009)
#define FRAMES 60

/* Tampons de travail : la zone SDK commence en 0x6C00, on reste loin devant. */
#define SRC 0x004800
#define DST 0x005000

#define PUSHALL __asm(" push XWA"); __asm(" push BC"); \
                __asm(" push XHL"); __asm(" push XDE");
#define POPALL  __asm(" pop XDE"); __asm(" pop XHL"); \
                __asm(" pop BC"); __asm(" pop XWA");

#define SETUP __asm(" ld XHL,0x004800"); __asm(" ld XDE,0x005000");

#define LDB64   SETUP __asm(" ld BC,64");  __asm(" ldirb (xde+),(xhl+)");
/* ⛔ UN BLOC COURT, ET C'EST TOUT LE POINT DE LA PAGE 1. Le vidage de file coute AU PLUS
 * un remplissage (~16 cy) par transfert, quelle que soit sa longueur. Avec des blocs de
 * 64 iterations il se noie dans 900 cycles de copie -- et la premiere version de cette
 * page rendait +0,4 cy, sous la resolution des compteurs. Avec 2 iterations (30 cy), le
 * vidage pese autant que la copie elle-meme. */
#define LDB2    SETUP __asm(" ld BC,2");   __asm(" ldirb (xde+),(xhl+)");
#define LDB128  SETUP __asm(" ld BC,128"); __asm(" ldirb (xde+),(xhl+)");
#define LDW64   SETUP __asm(" ld BC,64");  __asm(" ldirw (xde+),(xhl+)");
#define LDW128  SETUP __asm(" ld BC,128"); __asm(" ldirw (xde+),(xhl+)");

#define LOAD4 __asm(" ld XWA,0x01010101"); __asm(" ld XWA,0x01010101"); \
              __asm(" ld XWA,0x01010101"); __asm(" ld XWA,0x01010101");
#define LOAD8 LOAD4 LOAD4

/* ⛔ LES TROIS IMMEDIATS DOIVENT AVOIR LA MEME LONGUEUR. Une premiere version melangeait
 * `0x00000007` et `0x3FFF0000` : l'assembleur raccourcit les petites valeurs, donc les
 * rotations ne differaient plus seulement par les OPERANDES mais par le nombre d'octets
 * a fetcher -- et notre modele, dont la division est a latence FIXE, rendait pourtant
 * 75,6 / 81,8 / 83,8. Le montage mesurait l'encodage, pas la division.
 * ⇒ tous les dividendes >= 0x10000000, tous les diviseurs >= 0x1000, et les quotients
 * tiennent dans 16 bits (aucun debordement, donc aucune sortie anticipee). */
#define DIV0 __asm(" ld DE,0x8000"); __asm(" ld XWA,0x10000000"); __asm(" div XWA,DE");
#define DIV1 __asm(" ld DE,0x8000"); __asm(" ld XWA,0x7FFF0000"); __asm(" div XWA,DE");
#define DIV2 __asm(" ld DE,0x4000"); __asm(" ld XWA,0x10000000"); __asm(" div XWA,DE");

/* ⛔ DOUBLE DIFFERENCE, SINON ON MESURE L'INSTRUCTION ET PAS L'ETRANGLEMENT. Une
 * ecriture MOT et une ecriture OCTET n'ont ni le meme encodage ni le meme nombre
 * d'etats : comparer `V8W` a `V8B` melangerait cette difference-la avec le throttle.
 * ⇒ les MEMES instructions sont refaites en RAM (0x4200), ou il n'y a pas de throttle.
 *    (VRAM_octet - RAM_octet) et (VRAM_mot - RAM_mot) ne contiennent QUE l'etranglement.
 *    par ACCES  => les deux ecarts sont EGAUX
 *    par OCTET  => l'ecart du MOT est le DOUBLE de celui de l'octet */
#define VB1 __asm(" ld (0xBE00),A");
#define VW1 __asm(" ld (0xBE00),WA");
#define RB1 __asm(" ld (0x4200),A");
#define RW1 __asm(" ld (0x4200),WA");
#define VB8  VB1 VB1 VB1 VB1 VB1 VB1 VB1 VB1
#define VW8  VW1 VW1 VW1 VW1 VW1 VW1 VW1 VW1
#define RB8  RB1 RB1 RB1 RB1 RB1 RB1 RB1 RB1
#define RW8  RW1 RW1 RW1 RW1 RW1 RW1 RW1 RW1

#define X4(m)  m m m m
#define X16(m) X4(m) X4(m) X4(m) X4(m)

/* ⛔ UNE FONCTION PAR ROTATION, ET PAS UN PARAMETRE. Un `if` dans la boucle chaude
 * changerait le lot mesure d'une rotation a l'autre -- ce sont les DIFFERENCES entre
 * rotations qui portent toute l'information, elles doivent donc ne differer QUE par ce
 * qu'on veut mesurer. */
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

BATCH(m_b1,  X16(LDB64))
BATCH(m_b2,  X16(LDB128))
BATCH(m_w1,  X16(LDW64))
BATCH(m_w2,  X16(LDW128))

BATCH(m_q0,  X16(LDB2 LDB2 LDB2 LDB2))
BATCH(m_q4,  X16(LDB2 LOAD4 LDB2 LOAD4 LDB2 LOAD4 LDB2 LOAD4))
BATCH(m_r4,  X16(LOAD4 LOAD4 LOAD4 LOAD4))
BATCH(m_r8,  X16(LOAD8 LOAD8 LOAD8 LOAD8))

BATCH(m_d0,  X16(DIV0))
BATCH(m_d1,  X16(DIV1))
BATCH(m_d2,  X16(DIV2))

BATCH(m_v8b,  X16(VB8))
BATCH(m_v8w,  X16(VW8))
BATCH(m_r8b,  X16(RB8))
BATCH(m_r8w,  X16(RW8))

u8 rasv_max(void)
{
    u8 mx; u8 r; u16 s;
    mx = 0;
    for (s = 0; s < 40000; s++) { r = RAS_V; if (r > mx && r < 250) mx = r; }
    return mx;
}

#define PAL 0
#define P   SCR_1_PLANE
#define NPAGES 5

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
        PrintString(P, PAL, 1, 1, "LDIR B/W p");
        PrintString(P, PAL, 1, 3, "B1 ldirb64:");
        PrintString(P, PAL, 1, 4, "B2 ldirb128:");
        PrintString(P, PAL, 1, 5, "W1 ldirw64:");
        PrintString(P, PAL, 1, 6, "W2 ldirw128:");
    } else if (page == 1) {
        PrintString(P, PAL, 1, 1, "FILE/BLOC p");
        PrintString(P, PAL, 1, 3, "Q0 bloc   :");
        PrintString(P, PAL, 1, 4, "Q4 bloc+4 :");
        PrintString(P, PAL, 1, 5, "R4 4 seuls:");
        PrintString(P, PAL, 1, 6, "R8 8 seuls:");
    } else if (page == 2) {
        PrintString(P, PAL, 1, 1, "DIV OPER p");
        PrintString(P, PAL, 1, 3, "D0 petit  :");
        PrintString(P, PAL, 1, 4, "D1 grand q:");
        PrintString(P, PAL, 1, 5, "D2 grand/g:");
    } else if (page == 3) {
        PrintString(P, PAL, 1, 1, "VRAM A/O p");
        PrintString(P, PAL, 1, 3, "V8B  8 oct:");
        PrintString(P, PAL, 1, 4, "V8W  8 mot:");
        PrintString(P, PAL, 1, 5, "R8B ram oc:");
        PrintString(P, PAL, 1, 6, "R8W ram mo:");
    } else {
        PrintString(P, PAL, 1, 1, "VALIDITE p");
        PrintString(P, PAL, 1, 3, "RASV      :");
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

    /* ⛔ REPRIS DE LA v18/v19 : `CpuSpeed(0)` force le gear 0 (6,144 MHz). Sans lui la
     * ROM mesure une AUTRE horloge que les jeux et tous les chiffres sont a jeter sans
     * que rien ne le dise. */
    InitNGPC();
    SetBackgroundColour(RGB(2, 2, 4));
    SysSetSystemFont();
    SetPalette(P, PAL, 4, RGB(15, 15, 15), RGB(15, 15, 15), RGB(15, 15, 15));
    CpuSpeed(0);

    page = 0; shown = 255;

    for (;;) {
        if (page != shown) { draw_labels(page); shown = page; }

        if (page == 0) {
            PrintDecimal(P, PAL, 13, 3, m_b1(), 5);
            PrintDecimal(P, PAL, 13, 4, m_b2(), 5);
            PrintDecimal(P, PAL, 13, 5, m_w1(), 5);
            PrintDecimal(P, PAL, 13, 6, m_w2(), 5);
        } else if (page == 1) {
            PrintDecimal(P, PAL, 13, 3, m_q0(), 5);
            PrintDecimal(P, PAL, 13, 4, m_q4(), 5);
            PrintDecimal(P, PAL, 13, 5, m_r4(), 5);
            PrintDecimal(P, PAL, 13, 6, m_r8(), 5);
        } else if (page == 2) {
            PrintDecimal(P, PAL, 13, 3, m_d0(), 5);
            PrintDecimal(P, PAL, 13, 4, m_d1(), 5);
            PrintDecimal(P, PAL, 13, 5, m_d2(), 5);
        } else if (page == 3) {
            PrintDecimal(P, PAL, 13, 3, m_v8b(), 5);
            PrintDecimal(P, PAL, 13, 4, m_v8w(), 5);
            PrintDecimal(P, PAL, 13, 5, m_r8b(), 5);
            PrintDecimal(P, PAL, 13, 6, m_r8w(), 5);
        } else {
            PrintDecimal(P, PAL, 13, 3, (u16)rasv_max(), 5);
        }
        page = pad_page(page);
    }
}
