//+------------------------------------------------------------------+
//|                                       Gold_AI_Signal_Bot_EA.mq5  |
//|  Version autonome du robot Gold AI Signal Bot : tourne seule     |
//|  dans MetaTrader 5 (PC ou VPS MetaTrader), sans Python.          |
//|                                                                  |
//|  À la clôture de chaque bougie M5 :                              |
//|   - détecte 12 figures de chandeliers japonais,                  |
//|   - calcule le score de confluence /10 (tendance H1, EMA 20/50/  |
//|     200, zones support/résistance, RSI, ADX, session),           |
//|   - contrôle M15/H1/H4, volatilité, annonces, spread, limites,   |
//|   - place l'ordre (lot selon le risque, TP1 + TP2), puis gère :  |
//|     break-even, stop suiveur, sortie sur retournement, sortie    |
//|     au temps, pause sur perte journalière.                       |
//|  Mêmes règles que le moteur Python (analyzer/strategy.py).       |
//|                                                                  |
//|  Comptes DÉMO uniquement par défaut (InpAllowReal).              |
//+------------------------------------------------------------------+
#property copyright   "Gold AI Signal Bot"
#property link        "https://github.com/ck526438-lgtm/gold-ai-signal-bot"
#property version     "2.00"
#property description "Robot autonome or (XAUUSD) M5 : chandeliers japonais + confluence /10."
#property description "Place, gère et ferme ses trades seul. Démo uniquement par défaut."

#include <Trade\Trade.mqh>

//==================================================================== paramètres
input group "Robot"
input bool     InpTradingEnabled   = true;    // Autoriser le robot à trader
input bool     InpAutoTrade        = true;    // Placer automatiquement (false = bouton PLACER sur le graphique)
input bool     InpTakeAllSignals   = false;   // Tous les signaux (score ≥ 4, risque ÷ 2 sous le minimum, sans filtre de tendances)
input bool     InpAllowReal        = false;   // Autoriser un compte RÉEL (sinon démo uniquement)
input double   InpRiskPercent      = 0.5;     // Risque par trade (% du solde)
input double   InpMinScore         = 7.0;     // Score minimum pour trader (/10)
input int      InpMaxOpenTrades    = 1;       // Trades ouverts en même temps (max)
input int      InpMaxTradesPerDay  = 4;       // Trades par jour (max)
input double   InpDailyLossPct     = 2.0;     // Perte du jour avant pause (% du solde)
input double   InpMaxSpreadPips    = 6.0;     // Spread maximum (pips, or : 1 pip = 0,10 $)
input double   InpMaxSlippagePips  = 8.0;     // Écart maximum prix / signal (pips)
input double   InpMaxLots          = 1.0;     // Lot maximum par trade
input bool     InpSplitTargets     = true;    // Deux positions : TP1 et TP2
input bool     InpBreakeven        = true;    // Stop au point d'entrée après TP1
input bool     InpTimeExit         = true;    // Fermer après la durée maximale
input long     InpMagic            = 730526;  // Numéro magique

input group "Trades à score faible"
input bool     InpLowScore         = false;   // Trader aussi les scores faibles
input double   InpLowScoreMin      = 5.0;     // ... à partir du score
input double   InpLowScoreRisk     = 0.5;     // ... avec ce multiplicateur du risque

input group "Suivi des gains et sorties"
input bool     InpTrailing         = true;    // Stop suiveur
input double   InpTrailStartR      = 1.0;     // Démarre à un gain de (R)
input double   InpTrailDistR       = 0.7;     // Distance derrière le meilleur prix (R)
input bool     InpReversalExit     = true;    // Sortie sur signal de retournement
input double   InpReversalScore    = 6.0;     // Score minimum du signal inverse
input bool     InpReversalMomentum = false;   // ... ou perte d'élan (EMA 20 franchie, RSI / 50)
input bool     InpReversalStructure= false;   // ... ou cassure du dernier creux / sommet depuis l'entrée

input group "Encaisser le profit"
input double   InpQuickTargetR     = 0.0;     // Objectif unique en R (0 = TP1+TP2 ; 0,6 = petit ; 1,2 = moyen)

input group "Entrée"
input bool     InpPullback         = false;   // Entrée sur repli (sinon au marché)
input double   InpPullbackR        = 0.3;     // Repli attendu (fraction du risque)
input int      InpPullbackBars     = 3;       // Validité de l'entrée (bougies)

input group "Contrôle qualité"
input bool     InpCheckMTF         = true;    // M15 / H1 / H4 : aucune contre, 2 alignées
input bool     InpCheckNews        = true;    // Éviter les annonces USD fortes (NFP, CPI, Fed…)
input int      InpNewsWindowMin    = 30;      // Marge autour d'une annonce (min)
input double   InpMinAtrPips       = 10.0;     // Volatilité ATR minimum (pips)
input double   InpMaxAtrPips       = 120.0;    // Volatilité ATR maximum (pips)

input group "Stratégie (comme le moteur Python)"
input int      InpEmaFast          = 20;
input int      InpEmaMid           = 50;
input int      InpEmaSlow          = 200;
input int      InpRsiPeriod        = 14;
input int      InpAtrPeriod        = 14;
input int      InpAdxPeriod        = 14;
input double   InpAdxMin           = 18.0;
input int      InpSwingLookback    = 300;
input double   InpZoneAtrMult      = 0.6;
input double   InpSlAtrBuffer      = 0.25;
input double   InpMinSlPips        = 25.0;    // 2,50 $
input double   InpMaxSlPips        = 150.0;   // 15 $
input double   InpRrTp1            = 1.5;
input double   InpRrTp2            = 2.5;
input double   InpMinRoomR         = 1.2;
input int      InpCooldownBars     = 6;
input int      InpMaxHoldBars      = 48;
input bool     InpUseSessions      = true;    // Trader seulement pendant les sessions
input double   InpSess1Start       = 7;       // Session 1 début (heure UTC)
input double   InpSess1End         = 11;      // Session 1 fin (heure UTC)
input double   InpSess2Start       = 12;      // Session 2 début (heure UTC)
input double   InpSess2End         = 20;      // Session 2 fin (heure UTC, New York)
input int      InpBars             = 1500;    // Bougies M5 analysées
input int      InpGmtOffset        = -99;     // Décalage du courtier / UTC en heures (-99 = auto)
input int      InpTesterGmtOffset  = 2;       // Décalage utilisé dans le testeur de stratégie

input group "Affichage et notifications"
input bool     InpPush             = true;    // Notifications sur le téléphone (appli MT5)
input bool     InpAlerts           = false;   // Fenêtres d'alerte dans MT5
input bool     InpDraw             = true;    // Dessiner signaux et niveaux sur le graphique
input bool     InpPanel            = true;    // Panneau d'analyse en haut à gauche

//==================================================================== types
struct Pattern { string name; int dir; int strength; double extreme; };
struct Zone    { double price; int touches; bool support; };
struct Factor  { string label; double pts; };

struct Candidate
  {
   bool     ok;
   datetime time;        // heure d'ouverture de la bougie (serveur)
   int      dir;
   double   entry, sl, tp1, tp2, score, riskPips, atrPips;
   string   pattern;
   string   blocked;     // "" si valide
   int      m15, h1, h4;
   Factor   f[12];
   int      nf;
  };

struct Pending
  {
   string   key;
   datetime time;
   int      dir;
   double   level, sl, tp1, tp2, score;
   datetime expires;
   string   pattern;
  };

//==================================================================== état
CTrade   trade;
double   O[], H[], L[], C[];
datetime T[];
double   EF[], EM[], ES[], RSI[], ATR[], ADX[];
int      N = 0;
double   PIP = 0.0001;
datetime lastBar = 0;
datetime lastSig[2];                 // [0] achat, [1] vente : dernier signal valide (cooldown)
Pending  pend[];
string   lastPanel = "";
Candidate lastCand;
Candidate manualCand;                // signal en attente du bouton PLACER
datetime  manualUntil = 0;
string   pausedReason = "";
int      pausedDay = -1;
string   lastSkip = "";

//==================================================================== utilitaires
int    DirIdx(int d) { return d == 1 ? 0 : 1; }
string Side(int d)   { return d == 1 ? "ACHAT" : "VENTE"; }
double Pips(double x) { return x / PIP; }

int GmtOffset()
  {
   if(MQLInfoInteger(MQL_TESTER)) return InpTesterGmtOffset;
   if(InpGmtOffset != -99) return InpGmtOffset;
   return (int)MathRound((double)(TimeTradeServer() - TimeGMT()) / 3600.0);
  }

void Notify(string msg, bool push = true)
  {
   Print(msg);
   if(InpAlerts) Alert(msg);
   if(push && InpPush && TerminalInfoInteger(TERMINAL_NOTIFICATIONS_ENABLED))
      SendNotification(StringSubstr("XAUUSD M5 : " + msg, 0, 255));
  }

double FloorStep(double v, double step)
  {
   if(step <= 0) return v;
   return MathFloor(v / step + 1e-9) * step;
  }

string Key(datetime t, int d)
  {
   MqlDateTime m;
   TimeToStruct(t, m);
   return StringFormat("%04d%02d%02d%02d%02d%s", m.year, m.mon, m.day, m.hour, m.min, d == 1 ? "L" : "S");
  }

//==================================================================== indicateurs (identiques à indicators.py)
void Ema(const double &src[], double &dst[], int n, int len)
  {
   ArrayResize(dst, len);
   double a = 2.0 / (n + 1.0);
   dst[0] = src[0];
   for(int i = 1; i < len; i++) dst[i] = a * src[i] + (1 - a) * dst[i - 1];
  }

// moyenne lissée de Wilder ; démarre au premier élément valide (index `from`)
void Rma(const double &src[], double &dst[], int n, int len, int from)
  {
   ArrayResize(dst, len);
   double a = 1.0 / n;
   for(int i = 0; i < from && i < len; i++) dst[i] = EMPTY_VALUE;
   if(from >= len) return;
   dst[from] = src[from];
   for(int i = from + 1; i < len; i++) dst[i] = a * src[i] + (1 - a) * dst[i - 1];
  }

void ComputeIndicators()
  {
   Ema(C, EF, InpEmaFast, N);
   Ema(C, EM, InpEmaMid, N);
   Ema(C, ES, InpEmaSlow, N);
   // RSI
   double up[], dn[], ru[], rd[];
   ArrayResize(up, N); ArrayResize(dn, N);
   up[0] = 0; dn[0] = 0;
   for(int i = 1; i < N; i++) { double d = C[i] - C[i - 1]; up[i] = MathMax(d, 0); dn[i] = MathMax(-d, 0); }
   Rma(up, ru, InpRsiPeriod, N, 1);
   Rma(dn, rd, InpRsiPeriod, N, 1);
   ArrayResize(RSI, N);
   RSI[0] = 50;
   for(int i = 1; i < N; i++) RSI[i] = rd[i] == 0 ? 50 : 100 - 100 / (1 + ru[i] / rd[i]);
   // ATR
   double tr[];
   ArrayResize(tr, N);
   tr[0] = H[0] - L[0];
   for(int i = 1; i < N; i++)
      tr[i] = MathMax(H[i] - L[i], MathMax(MathAbs(H[i] - C[i - 1]), MathAbs(L[i] - C[i - 1])));
   Rma(tr, ATR, InpAtrPeriod, N, 0);
   // ADX
   double pdm[], mdm[], trs[], ps[], ms[], dx[];
   ArrayResize(pdm, N); ArrayResize(mdm, N); ArrayResize(dx, N);
   pdm[0] = 0; mdm[0] = 0;
   for(int i = 1; i < N; i++)
     {
      double u = H[i] - H[i - 1], d = L[i - 1] - L[i];
      pdm[i] = (u > d && u > 0) ? u : 0;
      mdm[i] = (d > u && d > 0) ? d : 0;
     }
   Rma(tr, trs, InpAdxPeriod, N, 0);
   Rma(pdm, ps, InpAdxPeriod, N, 0);
   Rma(mdm, ms, InpAdxPeriod, N, 0);
   for(int i = 0; i < N; i++)
     {
      double pdi = trs[i] > 0 ? 100 * ps[i] / trs[i] : 0, mdi = trs[i] > 0 ? 100 * ms[i] / trs[i] : 0;
      dx[i] = (pdi + mdi) > 0 ? 100 * MathAbs(pdi - mdi) / (pdi + mdi) : 0;
     }
   Rma(dx, ADX, InpAdxPeriod, N, 0);
  }

// tendance d'une unité de temps supérieure, sur ses bougies CLÔTURÉES uniquement
int TfBias(ENUM_TIMEFRAMES tf)
  {
   double cl[];
   int got = CopyClose(_Symbol, tf, 1, 300, cl);   // chronologique, sans la bougie en cours
   if(got < 60) return 0;
   double e20[], e50[];
   Ema(cl, e20, 20, got);
   Ema(cl, e50, 50, got);
   double c = cl[got - 1];
   if(c > e50[got - 1] && e20[got - 1] > e50[got - 1]) return 1;
   if(c < e50[got - 1] && e20[got - 1] < e50[got - 1]) return -1;
   return 0;
  }

//==================================================================== figures (identiques à patterns.py)
void AddPat(Pattern &res[], string name, int dir, int strength, double extreme)
  {
   int k = ArraySize(res);
   ArrayResize(res, k + 1);
   res[k].name = name; res[k].dir = dir; res[k].strength = strength; res[k].extreme = extreme;
  }

int DetectPatterns(int i, double atr, Pattern &res[])
  {
   ArrayResize(res, 0);
   if(i < 3 || atr <= 0) return 0;
   double b0 = MathAbs(C[i] - O[i]), r0 = MathMax(H[i] - L[i], 1e-12);
   double u0 = H[i] - MathMax(O[i], C[i]), lo0 = MathMin(O[i], C[i]) - L[i];
   double b1 = MathAbs(C[i - 1] - O[i - 1]), r1 = MathMax(H[i - 1] - L[i - 1], 1e-12);
   double b2 = MathAbs(C[i - 2] - O[i - 2]), r2 = MathMax(H[i - 2] - L[i - 2], 1e-12);
   bool bull0 = C[i] > O[i], bear0 = C[i] < O[i];
   bool bull1 = C[i - 1] > O[i - 1], bear1 = C[i - 1] < O[i - 1];
   bool meaningful = r0 >= 0.6 * atr;

   if(meaningful && bull0 && bear1 && C[i] >= O[i - 1] && O[i] <= C[i - 1] && b0 > b1 * 1.1)
      AddPat(res, "Avalement haussier", 1, b0 > atr ? 3 : 2, MathMin(L[i], L[i - 1]));
   if(meaningful && bear0 && bull1 && C[i] <= O[i - 1] && O[i] >= C[i - 1] && b0 > b1 * 1.1)
      AddPat(res, "Avalement baissier", -1, b0 > atr ? 3 : 2, MathMax(H[i], H[i - 1]));

   if(meaningful && lo0 >= 2.0 * b0 && lo0 >= 0.6 * r0 && u0 <= 0.25 * r0)
      AddPat(res, "Pin bar haussière (marteau)", 1, lo0 > 1.2 * atr ? 3 : 2, L[i]);
   if(meaningful && u0 >= 2.0 * b0 && u0 >= 0.6 * r0 && lo0 <= 0.25 * r0)
      AddPat(res, "Pin bar baissière (étoile filante)", -1, u0 > 1.2 * atr ? 3 : 2, H[i]);

   bool smallMid = b1 <= 0.35 * r1 || b1 <= 0.3 * atr;
   if(bull0 && C[i - 2] < O[i - 2] && b2 >= 0.6 * atr && smallMid && C[i] > (O[i - 2] + C[i - 2]) / 2)
      AddPat(res, "Étoile du matin", 1, 3, MathMin(L[i], MathMin(L[i - 1], L[i - 2])));
   if(bear0 && C[i - 2] > O[i - 2] && b2 >= 0.6 * atr && smallMid && C[i] < (O[i - 2] + C[i - 2]) / 2)
      AddPat(res, "Étoile du soir", -1, 3, MathMax(H[i], MathMax(H[i - 1], H[i - 2])));

   bool s3 = true, c3 = true;
   for(int k = i - 2; k <= i; k++)
     {
      if(!(C[k] > O[k] && MathAbs(C[k] - O[k]) >= 0.5 * atr)) s3 = false;
      if(!(C[k] < O[k] && MathAbs(C[k] - O[k]) >= 0.5 * atr)) c3 = false;
     }
   if(s3 && C[i] > C[i - 1] && C[i - 1] > C[i - 2]) AddPat(res, "Trois soldats blancs", 1, 2, L[i - 2]);
   if(c3 && C[i] < C[i - 1] && C[i - 1] < C[i - 2]) AddPat(res, "Trois corbeaux noirs", -1, 2, H[i - 2]);

   double tol = 0.1 * atr;
   if(bear1 && bull0 && MathAbs(L[i] - L[i - 1]) <= tol && meaningful)
      AddPat(res, "Pince basse (tweezer bottom)", 1, 2, MathMin(L[i], L[i - 1]));
   if(bull1 && bear0 && MathAbs(H[i] - H[i - 1]) <= tol && meaningful)
      AddPat(res, "Pince haute (tweezer top)", -1, 2, MathMax(H[i], H[i - 1]));

   if(H[i - 1] <= H[i - 2] && L[i - 1] >= L[i - 2] && r2 >= 0.8 * atr)
     {
      if(C[i] > H[i - 2] && bull0)      AddPat(res, "Cassure d'inside bar haussière", 1, 2, L[i - 1]);
      else if(C[i] < L[i - 2] && bear0) AddPat(res, "Cassure d'inside bar baissière", -1, 2, H[i - 1]);
     }
   return ArraySize(res);
  }

//==================================================================== zones (identiques à structure.py)
bool IsSwingHigh(int j, int k)
  {
   for(int m = j - k; m <= j + k; m++)
     {
      if(m < j && H[m] >= H[j]) return false;   // premier maximum au centre
      if(m > j && H[m] > H[j])  return false;
     }
   return true;
  }

bool IsSwingLow(int j, int k)
  {
   for(int m = j - k; m <= j + k; m++)
     {
      if(m < j && L[m] <= L[j]) return false;
      if(m > j && L[m] < L[j])  return false;
     }
   return true;
  }

int ZonesAt(int i, Zone &z[])
  {
   const int k = 3;
   ArrayResize(z, 0);
   double lv[];
   int n = 0;
   int from = MathMax(k, i - InpSwingLookback), to = i - k;
   for(int j = from; j <= to; j++)
     {
      if(j + k >= N) break;
      if(IsSwingHigh(j, k)) { ArrayResize(lv, n + 1); lv[n++] = H[j]; }
      if(IsSwingLow(j, k))  { ArrayResize(lv, n + 1); lv[n++] = L[j]; }
     }
   if(n == 0) return 0;
   ArraySort(lv);
   double tol = InpZoneAtrMult * ATR[i];
   double sum = lv[0];
   int cnt = 1;
   for(int m = 1; m <= n; m++)
     {
      bool flush = (m == n) || (lv[m] - sum / cnt > tol);
      if(flush)
        {
         int q = ArraySize(z);
         ArrayResize(z, q + 1);
         z[q].price = sum / cnt; z[q].touches = cnt; z[q].support = (sum / cnt) < C[i];
         if(m < n) { sum = lv[m]; cnt = 1; }
        }
      else { sum += lv[m]; cnt++; }
     }
   return ArraySize(z);
  }

// zone la plus proche de `price` parmi un type (support = true / résistance = false)
int Nearest(const Zone &z[], double price, bool support, bool onlyBeyond = false, double entry = 0, int dir = 0)
  {
   int best = -1;
   double bd = DBL_MAX;
   for(int q = 0; q < ArraySize(z); q++)
     {
      if(z[q].support != support) continue;
      if(onlyBeyond && (z[q].price - entry) * dir <= 0) continue;
      double d = MathAbs(z[q].price - price);
      if(d < bd) { bd = d; best = q; }
     }
   return best;
  }

//==================================================================== analyse (identique à strategy.py)
void AddF(Candidate &cd, string label, double pts)
  {
   if(cd.nf >= 12) return;
   cd.f[cd.nf].label = label; cd.f[cd.nf].pts = pts; cd.nf++;
  }

bool InSession(datetime closeUtc)
  {
   MqlDateTime t;
   TimeToStruct(closeUtc, t);
   double hr = t.hour + t.min / 60.0;
   return (hr >= InpSess1Start && hr < InpSess1End) || (hr >= InpSess2Start && hr < InpSess2End);
  }

Candidate Inspect(int i, int m15, int h1, int h4, double minScore)
  {
   Candidate cd;
   cd.ok = false; cd.nf = 0; cd.blocked = ""; cd.score = 0; cd.pattern = "";
   int warm = MathMax(InpEmaSlow, 60);
   if(i < warm) return cd;
   double atr = ATR[i];
   Pattern ps[];
   if(DetectPatterns(i, atr, ps) == 0) return cd;
   int bi = 0;
   for(int q = 1; q < ArraySize(ps); q++) if(ps[q].strength > ps[bi].strength) bi = q;
   int d = ps[bi].dir;
   bool conflict = false;
   int same = 0;
   string names = "";
   for(int q = 0; q < ArraySize(ps); q++)
     {
      if(ps[q].dir != d) { conflict = true; continue; }
      same++;
      if(StringFind(names, ps[q].name) < 0) names += (names == "" ? "" : " + ") + ps[q].name;
     }
   cd.ok = true; cd.dir = d; cd.time = T[i]; cd.pattern = ps[bi].name;
   cd.m15 = m15; cd.h1 = h1; cd.h4 = h4;

   AddF(cd, "Figure : " + names, MathMin(3, ps[bi].strength + (same > 1 ? 1 : 0)));
   if(h1 == d)       AddF(cd, "Dans le sens de la tendance H1", 2);
   else if(h1 == -d) AddF(cd, "Contre la tendance H1", -2);
   else              AddF(cd, "Tendance H1 neutre", 0);

   double c = C[i];
   bool aligned = (d == 1 && c > EM[i] && EF[i] > EM[i]) || (d == -1 && c < EM[i] && EF[i] < EM[i]);
   AddF(cd, aligned ? "EMA 20/50 M5 alignées" : "EMA 20/50 M5 non alignées", aligned ? 1 : 0);
   bool good200 = (d == 1 && c > ES[i]) || (d == -1 && c < ES[i]);
   AddF(cd, good200 ? "Prix du bon côté de l'EMA 200" : "Prix du mauvais côté de l'EMA 200", good200 ? 0.5 : 0);

   Zone z[];
   ZonesAt(i, z);
   int zi = Nearest(z, ps[bi].extreme, d == 1);
   if(zi >= 0 && MathAbs(ps[bi].extreme - z[zi].price) <= 0.5 * atr)
      AddF(cd, StringFormat("Rejet %s %s (%d touche%s)", d == 1 ? "du support" : "de la résistance",
                            DoubleToString(z[zi].price, _Digits), z[zi].touches, z[zi].touches > 1 ? "s" : ""),
           z[zi].touches >= 2 ? 2 : 1);
   else
     {
      double lo = MathMin(EF[i], EM[i]), hi = MathMax(EF[i], EM[i]);
      bool touched = (d == 1 && L[i] <= hi + 0.2 * atr && c > lo) || (d == -1 && H[i] >= lo - 0.2 * atr && c < hi);
      AddF(cd, touched ? "Rebond sur la zone EMA 20/50" : "Aucune zone clé touchée", touched ? 1 : 0);
     }

   double r = RSI[i], r1 = RSI[i - 1];
   if(d == 1)
     {
      if(r >= 75) AddF(cd, StringFormat("RSI suracheté (%.0f)", r), -1);
      else
        {
         bool ok = r > r1 && r < 70;
         AddF(cd, StringFormat("RSI %s (%.0f)", ok ? "remonte" : (r > r1 ? "remonte mais déjà élevé" : "ne confirme pas"), r), ok ? 1 : 0);
        }
     }
   else
     {
      if(r <= 25) AddF(cd, StringFormat("RSI survendu (%.0f)", r), -1);
      else
        {
         bool ok = r < r1 && r > 30;
         AddF(cd, StringFormat("RSI %s (%.0f)", ok ? "redescend" : (r < r1 ? "redescend mais déjà bas" : "ne confirme pas"), r), ok ? 1 : 0);
        }
     }

   if(ADX[i] >= InpAdxMin) AddF(cd, StringFormat("Marché directionnel (ADX %.0f)", ADX[i]), 1);
   else                    AddF(cd, StringFormat("Marché sans direction (ADX %.0f)", ADX[i]), -1);

   datetime closeUtc = T[i] + 300 - GmtOffset() * 3600;
   bool sess = InSession(closeUtc);
   AddF(cd, sess ? "Session Londres / New York" : "Hors session principale", sess ? 0.5 : 0);

   double sc = 0;
   for(int q = 0; q < cd.nf; q++) sc += cd.f[q].pts;
   cd.score = MathRound(MathMax(0, MathMin(10, sc)) * 10) / 10.0;

   // gestion du risque
   double entry = c;
   double sl = ps[bi].extreme - d * InpSlAtrBuffer * atr;
   double risk = MathAbs(entry - sl);
   if(risk < InpMinSlPips * PIP) { risk = InpMinSlPips * PIP; sl = entry - d * risk; }
   double tp1 = entry + d * InpRrTp1 * risk, tp2 = entry + d * InpRrTp2 * risk;

   int oi = Nearest(z, entry, d != 1, true, entry, d);
   if(oi >= 0)
     {
      double room = MathAbs(z[oi].price - entry);
      if(room < InpMinRoomR * risk)
         cd.blocked = StringFormat("Zone opposée trop proche (%s, %.1f R)", DoubleToString(z[oi].price, _Digits), room / risk);
      else if(room < InpRrTp2 * risk)
        {
         tp2 = z[oi].price - d * 0.1 * atr;
         if(MathAbs(tp2 - entry) < MathAbs(tp1 - entry)) tp1 = tp2;
        }
     }
   if(risk > InpMaxSlPips * PIP) cd.blocked = StringFormat("Stop trop large (%.1f pips > %.1f)", risk / PIP, InpMaxSlPips);
   if(cd.score < minScore)       cd.blocked = StringFormat("Score insuffisant (%.1f < %.1f)", cd.score, minScore);
   if(conflict)                  cd.blocked = "Figures achat et vente contradictoires";
   if(InpUseSessions && !sess)   cd.blocked = "Hors des heures de session configurées";

   cd.entry = NormalizeDouble(entry, _Digits); cd.sl = NormalizeDouble(sl, _Digits);
   cd.tp1 = NormalizeDouble(tp1, _Digits); cd.tp2 = NormalizeDouble(tp2, _Digits);
   cd.riskPips = MathRound(risk / PIP * 10) / 10; cd.atrPips = MathRound(atr / PIP * 10) / 10;
   return cd;
  }

double RobotMinScore()
  {
   if(InpTakeAllSignals) return MathMin(InpMinScore, 4.0);
   return InpLowScore ? MathMin(InpMinScore, InpLowScoreMin) : InpMinScore;
  }
double RiskFactor(double score)
  {
   if(score >= InpMinScore) return 1.0;
   if(InpTakeAllSignals) return MathMin(0.5, InpLowScore ? InpLowScoreRisk : 0.5);
   return InpLowScore ? InpLowScoreRisk : 1.0;
  }

//==================================================================== données
bool LoadBars()
  {
   MqlRates rt[];
   int got = CopyRates(_Symbol, PERIOD_M5, 1, InpBars, rt);   // bougies CLÔTURÉES, de la plus ancienne à la plus récente
   if(got < MathMax(InpEmaSlow, 60) + 10) return false;
   N = got;
   ArrayResize(O, N); ArrayResize(H, N); ArrayResize(L, N); ArrayResize(C, N); ArrayResize(T, N);
   for(int i = 0; i < N; i++) { O[i] = rt[i].open; H[i] = rt[i].high; L[i] = rt[i].low; C[i] = rt[i].close; T[i] = rt[i].time; }
   ComputeIndicators();
   return true;
  }

//==================================================================== compte, positions, statistiques
bool AccountOk(string &why, bool manual = false)
  {
   if(!manual && !InpTradingEnabled) { why = "robot désactivé (paramètre « Autoriser le robot à trader »)"; return false; }
   if(AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_REAL && !InpAllowReal)
     { why = "compte RÉEL : autorise-le dans les paramètres (InpAllowReal) — démo uniquement par défaut"; return false; }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)) { why = "bouton « Algo Trading » désactivé dans MetaTrader 5"; return false; }
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED)) { why = "trading automatique non autorisé pour ce robot (propriétés → Commun)"; return false; }
   if(!AccountInfoInteger(ACCOUNT_TRADE_EXPERT)) { why = "le courtier interdit le trading automatique sur ce compte"; return false; }
   return true;
  }

bool IsMine(ulong ticket)
  {
   if(!PositionSelectByTicket(ticket)) return false;
   return PositionGetString(POSITION_SYMBOL) == _Symbol && PositionGetInteger(POSITION_MAGIC) == InpMagic;
  }

int OpenKeys()
  {
   string keys[];
   int n = 0;
   for(int p = PositionsTotal() - 1; p >= 0; p--)
     {
      ulong tk = PositionGetTicket(p);
      if(!IsMine(tk)) continue;
      string k = PositionKey(PositionGetString(POSITION_COMMENT));
      bool seen = false;
      for(int q = 0; q < n; q++) if(keys[q] == k) seen = true;
      if(!seen) { ArrayResize(keys, n + 1); keys[n++] = k; }
     }
   return n + ArraySize(pend);
  }

string PositionKey(string comment)
  {
   // commentaire « M5A <clé> Lx »
   string parts[];
   if(StringSplit(comment, ' ', parts) >= 2) return parts[1];
   return comment;
  }

datetime DayStart() { MqlDateTime t; TimeToStruct(TimeTradeServer(), t); t.hour = 0; t.min = 0; t.sec = 0; return StructToTime(t); }

void TodayStats(double &realized, int &trades)
  {
   realized = 0; trades = 0;
   if(!HistorySelect(DayStart(), TimeTradeServer() + 60)) return;
   for(int k = HistoryDealsTotal() - 1; k >= 0; k--)
     {
      ulong d = HistoryDealGetTicket(k);
      if(HistoryDealGetInteger(d, DEAL_MAGIC) != InpMagic || HistoryDealGetString(d, DEAL_SYMBOL) != _Symbol) continue;
      long entry = HistoryDealGetInteger(d, DEAL_ENTRY);
      if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_INOUT)
         realized += HistoryDealGetDouble(d, DEAL_PROFIT) + HistoryDealGetDouble(d, DEAL_COMMISSION) + HistoryDealGetDouble(d, DEAL_SWAP);
      if(entry == DEAL_ENTRY_IN)
        {
         string cm = HistoryDealGetString(d, DEAL_COMMENT);
         if(StringFind(cm, " L2") < 0) trades++;      // une jambe TP2 ne compte pas comme un trade de plus
        }
     }
  }

double Floating()
  {
   double f = 0;
   for(int p = PositionsTotal() - 1; p >= 0; p--)
     {
      ulong tk = PositionGetTicket(p);
      if(IsMine(tk)) f += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
     }
   return f;
  }

bool CheckDailyLoss()
  {
   MqlDateTime t;
   TimeToStruct(TimeTradeServer(), t);
   if(pausedReason != "" && t.day_of_year != pausedDay) pausedReason = "";
   if(pausedReason != "") return false;
   double realized; int trades;
   TodayStats(realized, trades);
   double limit = AccountInfoDouble(ACCOUNT_BALANCE) * InpDailyLossPct / 100.0;
   if(realized + Floating() <= -limit)
     {
      pausedReason = StringFormat("pause : perte du jour %.2f ≥ %.2f %s (reprise demain)", -(realized + Floating()), limit,
                                  AccountInfoString(ACCOUNT_CURRENCY));
      pausedDay = t.day_of_year;
      CloseAll("perte journalière");
      Notify("Robot en " + pausedReason);
      return false;
     }
   return true;
  }

//==================================================================== annonces économiques (calendrier MQL5)
bool NewsBlocking(string &what)
  {
   if(!InpCheckNews || MQLInfoInteger(MQL_TESTER)) return false;   // calendrier indisponible dans le testeur
   datetime now = TimeTradeServer();
   string cur[1] = {"USD"};             // l'or réagit surtout aux annonces américaines
   for(int c = 0; c < 1; c++)
     {
      MqlCalendarValue v[];
      if(CalendarValueHistory(v, now - InpNewsWindowMin * 60, now + InpNewsWindowMin * 60, NULL, cur[c]) <= 0) continue;
      for(int k = 0; k < ArraySize(v); k++)
        {
         MqlCalendarEvent ev;
         if(!CalendarEventById(v[k].event_id, ev)) continue;
         if(ev.importance == CALENDAR_IMPORTANCE_HIGH)
           { what = StringFormat("%s %s à %s", cur[c], ev.name, TimeToString(v[k].time, TIME_MINUTES)); return true; }
        }
     }
   return false;
  }

//==================================================================== contrôles avant ordre (comme quality.py)
bool Quality(const Candidate &cd, string &why)
  {
   if(InpCheckMTF && !InpTakeAllSignals)
     {
      int against = 0, aligned = 0;
      int v[3]; v[0] = cd.m15; v[1] = cd.h1; v[2] = cd.h4;
      for(int k = 0; k < 3; k++) { if(v[k] == -cd.dir) against++; if(v[k] == cd.dir) aligned++; }
      if(against > 0 || aligned < 2)
        { why = StringFormat("tendances M15 %d / H1 %d / H4 %d pas assez alignées", cd.m15, cd.h1, cd.h4); return false; }
     }
   if(cd.atrPips < InpMinAtrPips || cd.atrPips > InpMaxAtrPips)
     { why = StringFormat("volatilité ATR %.1f pips hors plage %.1f–%.1f", cd.atrPips, InpMinAtrPips, InpMaxAtrPips); return false; }
   string news;
   if(NewsBlocking(news)) { why = "annonce économique proche : " + news; return false; }
   return true;
  }

void Skip(const Candidate &cd, string why)
  {
   lastSkip = StringFormat("%s %s %.1f/10 non tradé : %s", Side(cd.dir), cd.pattern, cd.score, why);
   Notify(lastSkip, false);
  }

//==================================================================== ordres
bool Place(const Candidate &cd, double entryRef, bool manual = false)
  {
   int d = cd.dir;
   MqlTick tk;
   if(!SymbolInfoTick(_Symbol, tk)) { Skip(cd, "pas de prix"); return false; }
   double price = d == 1 ? tk.ask : tk.bid;
   double spread = Pips(tk.ask - tk.bid);
   if(spread > InpMaxSpreadPips) { Skip(cd, StringFormat("spread trop élevé (%.1f pips)", spread)); return false; }
   if(manual)
     {
      if((cd.tp1 - price) * d < 0.5 * MathAbs(price - cd.sl)) { Skip(cd, "le prix est déjà trop près de l'objectif"); return false; }
     }
   else if(Pips(MathAbs(price - entryRef)) > InpMaxSlippagePips)
     { Skip(cd, StringFormat("le prix a trop bougé depuis le signal (%.1f pips)", Pips(MathAbs(price - entryRef)))); return false; }
   if((price - cd.sl) * d <= 0 || (cd.tp1 - price) * d <= 0) { Skip(cd, "stop ou objectif déjà dépassé"); return false; }

   double lossPerLot = 0;
   if(!OrderCalcProfit(d == 1 ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, _Symbol, 1.0, price, cd.sl, lossPerLot) || lossPerLot == 0)
     { Skip(cd, "valeur du pip introuvable"); return false; }
   lossPerLot = MathAbs(lossPerLot);
   double riskMoney = AccountInfoDouble(ACCOUNT_BALANCE) * InpRiskPercent / 100.0 * RiskFactor(cd.score);
   double vmin = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN), vstep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double vmax = MathMin(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX), InpMaxLots);
   double lots = MathMin(FloorStep(riskMoney / lossPerLot, vstep), vmax);
   if(lots < vmin)
     { Skip(cd, StringFormat("risque trop faible pour le lot minimum %.2f (il faudrait %.2f %s)", vmin, vmin * lossPerLot,
                             AccountInfoString(ACCOUNT_CURRENCY))); return false; }

   double v1 = lots, v2 = 0;
   bool split = InpSplitTargets && MathAbs(cd.tp2 - cd.tp1) > _Point;
   if(split)
     {
      v1 = FloorStep(lots / 2, vstep);
      v2 = NormalizeDouble(lots - v1, 2);
      if(v1 < vmin || v2 < vmin) { split = false; v1 = lots; v2 = 0; }
     }
   string key = Key(cd.time, d);
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   double sl = NormalizeDouble(cd.sl, digits);
   bool okAll = true;
   for(int leg = split ? 1 : 0; leg <= (split ? 2 : 0); leg++)
     {
      double vol = leg == 2 ? v2 : v1;
      double tp = NormalizeDouble(leg == 1 ? cd.tp1 : cd.tp2, digits);
      string cm = StringFormat("M5A %s L%d", key, leg);
      bool sent = d == 1 ? trade.Buy(vol, _Symbol, 0, sl, tp, cm) : trade.Sell(vol, _Symbol, 0, sl, tp, cm);
      uint rc = trade.ResultRetcode();
      if(!sent || (rc != TRADE_RETCODE_DONE && rc != TRADE_RETCODE_PLACED && rc != TRADE_RETCODE_DONE_PARTIAL))
        {
         okAll = false;
         Notify(StringFormat("Ordre %s refusé par le courtier : %s (%u)", Side(d), trade.ResultRetcodeDescription(), rc));
         continue;
        }
      ulong deal = trade.ResultDeal();
      ulong pos = 0;
      if(deal > 0 && HistoryDealSelect(deal)) pos = (ulong)HistoryDealGetInteger(deal, DEAL_POSITION_ID);
      if(pos == 0) pos = trade.ResultOrder();
      GlobalVariableSet(StringFormat("M5A_%I64u_sl0", pos), sl);
      GlobalVariableSet(StringFormat("M5A_%I64u_tp1", pos), cd.tp1);
      GlobalVariableSet(StringFormat("M5A_%I64u_peak", pos), trade.ResultPrice());
      Notify(StringFormat("%s %.2f lot à %s — stop %s, objectif %s (%s %.1f/10)", Side(d), vol,
                          DoubleToString(trade.ResultPrice(), digits), DoubleToString(sl, digits),
                          DoubleToString(tp, digits), cd.pattern, cd.score));
     }
   if(InpDraw) DrawSignal(cd, true);
   return okAll;
  }

void CloseAll(string why)
  {
   for(int p = PositionsTotal() - 1; p >= 0; p--)
     {
      ulong tk = PositionGetTicket(p);
      if(!IsMine(tk)) continue;
      if(trade.PositionClose(tk)) Notify(StringFormat("Position #%I64u fermée : %s", tk, why));
     }
   ArrayResize(pend, 0);
  }

//==================================================================== gestion des positions (chaque tick)
void ManagePositions()
  {
   MqlTick tk;
   if(!SymbolInfoTick(_Symbol, tk)) return;
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   double stopsLvl = MathMax((double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL), 1.0) * _Point;
   for(int p = PositionsTotal() - 1; p >= 0; p--)
     {
      ulong tkt = PositionGetTicket(p);
      if(!IsMine(tkt)) continue;
      int d = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? 1 : -1;
      double open = PositionGetDouble(POSITION_PRICE_OPEN), sl = PositionGetDouble(POSITION_SL), tp = PositionGetDouble(POSITION_TP);
      datetime opened = (datetime)PositionGetInteger(POSITION_TIME);
      string cm = PositionGetString(POSITION_COMMENT);
      string gsl = StringFormat("M5A_%I64u_sl0", tkt), gpk = StringFormat("M5A_%I64u_peak", tkt), gt1 = StringFormat("M5A_%I64u_tp1", tkt);
      double sl0 = GlobalVariableCheck(gsl) ? GlobalVariableGet(gsl) : sl;
      double tp1 = GlobalVariableCheck(gt1) ? GlobalVariableGet(gt1) : 0;
      double risk = MathAbs(open - sl0);
      double price = d == 1 ? tk.bid : tk.ask;

      // sortie au temps
      if(InpTimeExit && TimeTradeServer() >= opened + InpMaxHoldBars * 300)
        {
         if(trade.PositionClose(tkt)) Notify(StringFormat("Position #%I64u fermée après %d bougies (%.2f)", tkt, InpMaxHoldBars, PositionGetDouble(POSITION_PROFIT)));
         continue;
        }
      // meilleur prix atteint
      double peak = GlobalVariableCheck(gpk) ? GlobalVariableGet(gpk) : open;
      if((price - peak) * d > 0) { peak = price; GlobalVariableSet(gpk, peak); }
      double newSl = sl;
      // break-even de la jambe TP2 quand TP1 a été atteint
      if(InpBreakeven && StringFind(cm, " L2") >= 0 && tp1 > 0 && (peak - tp1) * d >= 0 && (sl == 0 || (sl - open) * d < 0))
         newSl = open;
      // stop suiveur
      if(InpTrailing && risk > 0 && (peak - open) * d >= InpTrailStartR * risk)
        {
         double t = peak - d * InpTrailDistR * risk;
         if((price - t) * d < stopsLvl) t = price - d * stopsLvl;
         if((t - newSl) * d > 0.1 * risk || newSl == 0) newSl = MathMax(newSl, 0) == 0 ? t : (d == 1 ? MathMax(newSl, t) : MathMin(newSl, t));
        }
      newSl = NormalizeDouble(newSl, digits);
      if(newSl != NormalizeDouble(sl, digits) && (newSl - sl) * d > 0 && (price - newSl) * d >= stopsLvl)
        {
         if(trade.PositionModify(tkt, newSl, tp))
           {
            double locked = risk > 0 ? (newSl - open) * d / risk : 0;
            bool first = risk > 0 && (sl - open) * d < 0.25 * risk;
            string msg = StringFormat("Stop #%I64u → %s (gain verrouillé %+.2f R)", tkt, DoubleToString(newSl, digits), locked);
            Notify(msg, first);
           }
        }
     }
   // nettoyage des variables globales des positions fermées
   static datetime lastClean = 0;
   if(TimeCurrent() - lastClean > 3600)
     {
      lastClean = TimeCurrent();
      for(int g = GlobalVariablesTotal() - 1; g >= 0; g--)
        {
         string nm = GlobalVariableName(g);
         if(StringFind(nm, "M5A_") != 0) continue;
         string parts[];
         if(StringSplit(nm, '_', parts) < 3) continue;
         ulong t = (ulong)StringToInteger(parts[1]);
         if(!PositionSelectByTicket(t)) GlobalVariableDel(nm);
        }
     }
  }

// dernier creux (low = true) ou sommet confirmé à la clôture de la bougie i (3 bougies de chaque côté)
int LastSwing(int i, bool low)
  {
   for(int j = i - 3; j >= 3; j--)
      if(low ? IsSwingLow(j, 3) : IsSwingHigh(j, 3)) return j;
   return -1;
  }

// raison de couper une position de sens d ouverte à `opened`, "" sinon (comme backtest.reversal_reason)
string ReversalReason(const Candidate &cd, int i, int d, datetime opened)
  {
   if(cd.ok && cd.dir == -d && cd.score >= InpReversalScore && cd.blocked != "Figures achat et vente contradictoires")
      return StringFormat("%s %.1f/10 en sens inverse", cd.pattern, cd.score);
   if(InpReversalMomentum && i >= 1)
     {
      double body = MathAbs(C[i] - O[i]);
      bool m = d == 1 ? (C[i] < EF[i] && C[i - 1] >= EF[i - 1] && RSI[i] < 50 && C[i] < O[i] && body >= 0.3 * ATR[i])
                      : (C[i] > EF[i] && C[i - 1] <= EF[i - 1] && RSI[i] > 50 && C[i] > O[i] && body >= 0.3 * ATR[i]);
      if(m) return d == 1 ? "perte d'élan : clôture sous l'EMA 20, RSI sous 50" : "perte d'élan : clôture au-dessus de l'EMA 20, RSI au-dessus de 50";
     }
   if(InpReversalStructure)
     {
      int s = LastSwing(i, d == 1);
      if(s >= 0 && T[s] >= opened)
        {
         double lvl = d == 1 ? L[s] : H[s];
         if((C[i] - lvl) * d < 0)
            return StringFormat("cassure du dernier %s %s", d == 1 ? "creux" : "sommet", DoubleToString(lvl, _Digits));
        }
     }
   return "";
  }

void Reversal(const Candidate &cd, int i)
  {
   if(!InpReversalExit) return;
   datetime closeT = T[i] + 300;
   for(int p = PositionsTotal() - 1; p >= 0; p--)
     {
      ulong tkt = PositionGetTicket(p);
      if(!IsMine(tkt)) continue;
      int d = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? 1 : -1;
      datetime opened = (datetime)PositionGetInteger(POSITION_TIME);
      if(opened > closeT) continue;
      string why = ReversalReason(cd, i, d, opened - opened % 300);
      if(why == "") continue;
      double pr = PositionGetDouble(POSITION_PROFIT);
      if(trade.PositionClose(tkt))
         Notify(StringFormat("Retournement détecté (%s) — position #%I64u coupée (%.2f)", why, tkt, pr));
     }
  }

//==================================================================== entrée sur repli
void ArmPullback(const Candidate &cd)
  {
   int k = ArraySize(pend);
   ArrayResize(pend, k + 1);
   pend[k].key = Key(cd.time, cd.dir); pend[k].time = cd.time; pend[k].dir = cd.dir; pend[k].sl = cd.sl; pend[k].tp1 = cd.tp1; pend[k].tp2 = cd.tp2;
   pend[k].score = cd.score; pend[k].pattern = cd.pattern;
   pend[k].level = NormalizeDouble(cd.entry - cd.dir * InpPullbackR * MathAbs(cd.entry - cd.sl), _Digits);
   pend[k].expires = cd.time + 300 + InpPullbackBars * 300;
   Notify(StringFormat("%s %s %.1f/10 : entrée en attente d'un repli à %s jusqu'à %s", Side(cd.dir), cd.pattern, cd.score,
                       DoubleToString(pend[k].level, _Digits), TimeToString(pend[k].expires, TIME_MINUTES)));
   if(InpDraw) DrawSignal(cd, false);
  }

void WatchPending()
  {
   if(ArraySize(pend) == 0) return;
   MqlTick tk;
   if(!SymbolInfoTick(_Symbol, tk)) return;
   for(int k = ArraySize(pend) - 1; k >= 0; k--)
     {
      int d = pend[k].dir;
      double price = d == 1 ? tk.ask : tk.bid;
      string why = "";
      bool fire = false;
      if((price - pend[k].tp1) * d >= 0)         why = "TP1 atteint sans repli — entrée annulée";
      else if((price - pend[k].sl) * d <= 0)     why = "stop atteint avant l'entrée — entrée annulée";
      else if((price - pend[k].level) * d <= 0)  fire = true;
      else if(TimeTradeServer() >= pend[k].expires) why = "repli non atteint à temps — entrée annulée";
      if(!fire && why == "") continue;
      Candidate cd;
      cd.ok = true; cd.dir = d; cd.sl = pend[k].sl; cd.tp1 = pend[k].tp1; cd.tp2 = pend[k].tp2; cd.score = pend[k].score;
      cd.pattern = pend[k].pattern; cd.entry = pend[k].level; cd.nf = 0; cd.blocked = "";
      cd.time = pend[k].time;
      for(int m = k; m < ArraySize(pend) - 1; m++) pend[m] = pend[m + 1];
      ArrayResize(pend, ArraySize(pend) - 1);
      if(fire)
        {
         string w;
         if(AccountOk(w) && CheckDailyLoss()) Place(cd, cd.entry);
        }
      else Skip(cd, why);
     }
  }

//==================================================================== nouvelle bougie
void OnNewBar()
  {
   if(!LoadBars()) { Print("Historique M5 insuffisant"); return; }
   int i = N - 1;
   int m15 = TfBias(PERIOD_M15), h1 = TfBias(PERIOD_H1), h4 = TfBias(PERIOD_H4);
   double minS = RobotMinScore();
   Candidate cd = Inspect(i, m15, h1, h4, minS);
   lastCand = cd;
   Reversal(cd, i);
   if(!cd.ok || cd.blocked != "") { UpdatePanel(); return; }

   int di = DirIdx(cd.dir);
   if(lastSig[di] > 0 && (cd.time - lastSig[di]) / 300 < InpCooldownBars) { UpdatePanel(); return; }
   lastSig[di] = cd.time;                       // même règle que scan() : délai entre signaux valides
   if(InpDraw) DrawSignal(cd, false);
   Notify(StringFormat("Signal %s %s — score %.1f/10, entrée %s, stop %s, TP1 %s, TP2 %s", Side(cd.dir), cd.pattern, cd.score,
                       DoubleToString(cd.entry, _Digits), DoubleToString(cd.sl, _Digits), DoubleToString(cd.tp1, _Digits), DoubleToString(cd.tp2, _Digits)));

   string why;
   if(!InpAutoTrade)                             // mode manuel : bouton PLACER sur le graphique
     {
      if(InpQuickTargetR > 0)
        {
         double tq = NormalizeDouble(cd.entry + cd.dir * InpQuickTargetR * MathAbs(cd.entry - cd.sl), _Digits);
         cd.tp1 = tq; cd.tp2 = tq;
        }
      manualCand = cd;
      manualUntil = cd.time + 300 + 30 * 60;
      string qual = Quality(cd, why) ? "" : " (le robot ne l'aurait pas pris : " + why + ")";
      ShowManualButtons();
      Notify(StringFormat("Signal %s %s %.1f/10 : appuie sur PLACER dans MT5 (valable 30 min)%s", Side(cd.dir), cd.pattern, cd.score, qual));
      UpdatePanel();
      return;
     }
   if(!AccountOk(why))     { Skip(cd, why); UpdatePanel(); return; }
   if(!CheckDailyLoss())   { Skip(cd, pausedReason); UpdatePanel(); return; }
   double realized; int today;
   TodayStats(realized, today);
   if(today + ArraySize(pend) >= InpMaxTradesPerDay) { Skip(cd, StringFormat("limite de %d trades par jour", InpMaxTradesPerDay)); UpdatePanel(); return; }
   if(OpenKeys() >= InpMaxOpenTrades) { Skip(cd, StringFormat("déjà %d trade(s) ouvert(s)", InpMaxOpenTrades)); UpdatePanel(); return; }
   if(!Quality(cd, why))   { Skip(cd, why); UpdatePanel(); return; }

   if(InpQuickTargetR > 0)                       // encaisser vite : objectif unique proche
     {
      double tp = NormalizeDouble(cd.entry + cd.dir * InpQuickTargetR * MathAbs(cd.entry - cd.sl), _Digits);
      cd.tp1 = tp; cd.tp2 = tp;
     }
   if(InpPullback) ArmPullback(cd);
   else            Place(cd, cd.entry);
   UpdatePanel();
  }

//==================================================================== mode manuel : bouton PLACER
void Button(string name, string text, int x, int y, int w, color bg)
  {
   if(ObjectFind(0, name) < 0) ObjectCreate(0, name, OBJ_BUTTON, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_RIGHT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, name, OBJPROP_XSIZE, w);
   ObjectSetInteger(0, name, OBJPROP_YSIZE, 34);
   ObjectSetInteger(0, name, OBJPROP_BGCOLOR, bg);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clrWhite);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 10);
   ObjectSetInteger(0, name, OBJPROP_STATE, false);
   ObjectSetString(0, name, OBJPROP_TEXT, text);
  }

void ShowManualButtons()
  {
   Button("M5A_btn_place", StringFormat("PLACER %s %.1f/10", Side(manualCand.dir), manualCand.score), 250, 30, 230,
          manualCand.dir == 1 ? clrSeaGreen : clrCrimson);
   Button("M5A_btn_skip", "IGNORER", 250, 70, 230, clrDimGray);
   ChartRedraw();
  }

void HideManualButtons()
  {
   ObjectDelete(0, "M5A_btn_place");
   ObjectDelete(0, "M5A_btn_skip");
   manualUntil = 0;
   ChartRedraw();
  }

void PlaceManual()
  {
   if(manualUntil == 0 || TimeTradeServer() > manualUntil) { HideManualButtons(); Notify("Signal expiré : rien n'a été placé", false); return; }
   string why;
   if(!AccountOk(why, true)) { Skip(manualCand, why); return; }
   if(!CheckDailyLoss())     { Skip(manualCand, pausedReason); return; }
   if(Place(manualCand, manualCand.entry, true)) Notify("Ordre MANUEL placé avec le bouton PLACER");
   HideManualButtons();
   UpdatePanel();
  }

void OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam)
  {
   if(id != CHARTEVENT_OBJECT_CLICK) return;
   if(sparam == "M5A_btn_place") PlaceManual();
   else if(sparam == "M5A_btn_skip") { HideManualButtons(); Print("Signal ignoré"); }
  }

//==================================================================== affichage
void DrawSignal(const Candidate &cd, bool executed)
  {
   string base = "M5A_" + Key(cd.time, cd.dir);
   string ar = base + "_arrow";
   if(ObjectFind(0, ar) < 0)
     {
      double y = cd.dir == 1 ? iLow(_Symbol, PERIOD_M5, 1) - 15 * PIP : iHigh(_Symbol, PERIOD_M5, 1) + 15 * PIP;
      ObjectCreate(0, ar, OBJ_ARROW, 0, cd.time, y);
      ObjectSetInteger(0, ar, OBJPROP_ARROWCODE, cd.dir == 1 ? 233 : 234);
      ObjectSetInteger(0, ar, OBJPROP_COLOR, cd.dir == 1 ? clrMediumSeaGreen : clrTomato);
      ObjectSetInteger(0, ar, OBJPROP_WIDTH, 2);
      ObjectSetString(0, ar, OBJPROP_TOOLTIP, StringFormat("%s %s %.1f/10", Side(cd.dir), cd.pattern, cd.score));
     }
   datetime t2 = cd.time + InpMaxHoldBars * 300;
   string nm[3] = {"_sl", "_tp1", "_tp2"};
   double pr[3]; pr[0] = cd.sl; pr[1] = cd.tp1; pr[2] = cd.tp2;
   color col[3] = {clrTomato, clrMediumSeaGreen, clrSeaGreen};
   for(int k = 0; k < 3; k++)
     {
      string o = base + nm[k];
      if(ObjectFind(0, o) < 0) ObjectCreate(0, o, OBJ_TREND, 0, cd.time, pr[k], t2, pr[k]);
      ObjectSetInteger(0, o, OBJPROP_COLOR, col[k]);
      ObjectSetInteger(0, o, OBJPROP_STYLE, executed ? STYLE_SOLID : STYLE_DOT);
      ObjectSetInteger(0, o, OBJPROP_RAY_RIGHT, false);
     }
   ChartRedraw();
  }

void UpdatePanel()
  {
   if(!InpPanel) return;
   string s = "Gold AI Signal Bot — robot autonome\n";
   string why;
   bool ok = AccountOk(why);
   s += (AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_REAL ? "Compte RÉEL" : "Compte démo") +
        StringFormat("  ·  solde %.2f %s  ·  risque %.2f %%\n", AccountInfoDouble(ACCOUNT_BALANCE), AccountInfoString(ACCOUNT_CURRENCY), InpRiskPercent);
   s += ok ? (pausedReason != "" ? "État : " + pausedReason + "\n" : "État : ACTIF — trade les signaux ≥ " + DoubleToString(RobotMinScore(), 1) + "/10\n")
           : "État : NE TRADE PAS — " + why + "\n";
   double realized; int today;
   TodayStats(realized, today);
   s += StringFormat("Aujourd'hui : %d / %d trades · réalisé %.2f · en cours %.2f\n", today, InpMaxTradesPerDay, realized, Floating());
   s += "──────────────\n";
   if(lastCand.ok)
     {
      s += StringFormat("Dernière bougie : %s %s — %.1f/10 %s\n", Side(lastCand.dir), lastCand.pattern, lastCand.score,
                        lastCand.blocked == "" ? "(valide)" : "(" + lastCand.blocked + ")");
      for(int k = 0; k < lastCand.nf; k++)
         s += StringFormat("   %s%s  %s\n", lastCand.f[k].pts > 0 ? "+" : (lastCand.f[k].pts < 0 ? "" : " "),
                           DoubleToString(lastCand.f[k].pts, 1), lastCand.f[k].label);
      s += StringFormat("   Tendances M15 %s · H1 %s · H4 %s · ATR %.1f pips\n", Arrow(lastCand.m15), Arrow(lastCand.h1), Arrow(lastCand.h4), lastCand.atrPips);
     }
   else s += "Dernière bougie : aucune figure de chandelier\n";
   if(ArraySize(pend) > 0)
      for(int k = 0; k < ArraySize(pend); k++)
         s += StringFormat("En attente : %s à %s (jusqu'à %s)\n", Side(pend[k].dir), DoubleToString(pend[k].level, _Digits), TimeToString(pend[k].expires, TIME_MINUTES));
   if(manualUntil > 0)
      s += StringFormat("À PLACER : %s %s %.1f/10 — bouton PLACER jusqu'à %s\n", Side(manualCand.dir), manualCand.pattern,
                        manualCand.score, TimeToString(manualUntil, TIME_MINUTES));
   if(lastSkip != "") s += "Dernier refus : " + lastSkip + "\n";
   if(s != lastPanel) { Comment(s); lastPanel = s; }
  }

string Arrow(int v) { return v == 1 ? "▲" : (v == -1 ? "▼" : "↔"); }

//==================================================================== cycle de vie
int OnInit()
  {
   if(_Period != PERIOD_M5) Print("Conseil : attache le robot à un graphique M5 (il analyse toujours le M5).");
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   // or : 1 pip = 0,10 $ quel que soit le nombre de décimales du courtier (2 ou 3)
   PIP = (StringFind(_Symbol, "XAU") >= 0 || StringFind(_Symbol, "GOLD") >= 0) ? 0.1
         : ((digits == 3 || digits == 5) ? 10 * _Point : _Point);
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(30);
   trade.SetTypeFillingBySymbol(_Symbol);
   lastSig[0] = 0; lastSig[1] = 0;
   lastCand.ok = false;
   manualCand.ok = false;
   EventSetTimer(1);                     // entrées en attente et panneau, même sans tick
   string why;
   Notify(StringFormat("Robot démarré sur %s (%s) — %s", _Symbol,
                       AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_REAL ? "compte RÉEL" : "compte démo",
                       AccountOk(why) ? "actif" : "ne trade pas : " + why), false);
   lastBar = iTime(_Symbol, PERIOD_M5, 0);
   if(LoadBars())
     {
      lastCand = Inspect(N - 1, TfBias(PERIOD_M15), TfBias(PERIOD_H1), TfBias(PERIOD_H4), RobotMinScore());
     }
   UpdatePanel();
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   HideManualButtons();
   Comment("");
  }

void OnTick()
  {
   datetime bar = iTime(_Symbol, PERIOD_M5, 0);
   if(bar != 0 && bar != lastBar)
     {
      lastBar = bar;
      OnNewBar();
     }
   WatchPending();
   ManagePositions();
  }

void OnTimer()
  {
   if(manualUntil > 0 && TimeTradeServer() > manualUntil) HideManualButtons();
   WatchPending();
   static int n = 0;
   if(++n % 5 == 0) UpdatePanel();
  }
//+------------------------------------------------------------------+
