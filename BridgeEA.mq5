#property strict
#include <Trade\Trade.mqh>

CTrade trade;
input string InpPrefix = "bridge_";
string cmdFile;
string resFile;
string stateFile;
datetime lastState = 0;

int OnInit()
{
   cmdFile = InpPrefix + "cmd.json";
   resFile = InpPrefix + "res.json";
   stateFile = InpPrefix + "state.json";
   trade.SetExpertMagicNumber(0);
   trade.SetDeviationInPoints(20);
   EventSetMillisecondTimer(250);
   WriteState();
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   if (TimeLocal() - lastState >= 1)
   {
      WriteState();
      lastState = TimeLocal();
   }
   if (!FileIsExist(cmdFile))
      return;
   string raw = ReadText(cmdFile);
   FileDelete(cmdFile);
   if (StringLen(raw) < 2)
      return;
   Process(raw);
}

string ReadText(const string name)
{
   int h = FileOpen(name, FILE_READ | FILE_TXT | FILE_ANSI);
   if (h == INVALID_HANDLE)
      return "";
   string s = "";
   while (!FileIsEnding(h))
      s += FileReadString(h) + "\n";
   FileClose(h);
   return s;
}

void WriteText(const string name, const string text)
{
   int h = FileOpen(name, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if (h == INVALID_HANDLE)
      return;
   FileWriteString(h, text);
   FileClose(h);
}

string JsonStr(const string js, const string key)
{
   string pat = "\"" + key + "\"";
   int p = StringFind(js, pat);
   if (p < 0)
      return "";
   p = StringFind(js, ":", p + StringLen(pat));
   if (p < 0)
      return "";
   p++;
   while (p < StringLen(js) && (StringGetCharacter(js, p) == ' ' || StringGetCharacter(js, p) == '\t'))
      p++;
   if (p >= StringLen(js))
      return "";
   ushort c = StringGetCharacter(js, p);
   if (c == '"')
   {
      p++;
      int e = p;
      while (e < StringLen(js) && StringGetCharacter(js, e) != '"')
         e++;
      return StringSubstr(js, p, e - p);
   }
   int e = p;
   while (e < StringLen(js) && StringGetCharacter(js, e) != ',' && StringGetCharacter(js, e) != '}')
      e++;
   return StringSubstr(js, p, e - p);
}

double JsonNum(const string js, const string key)
{
   string v = JsonStr(js, key);
   if (v == "")
      return 0.0;
   return StringToDouble(v);
}

void Process(const string js)
{
   string id = JsonStr(js, "id");
   string op = JsonStr(js, "op");
   string symbol = JsonStr(js, "symbol");
   if (symbol == "")
      symbol = _Symbol;
   double volume = JsonNum(js, "volume");
   string type = JsonStr(js, "type");
   double price = JsonNum(js, "price");
   double sl = JsonNum(js, "sl");
   double tp = JsonNum(js, "tp");
   long ticket = (long)JsonNum(js, "ticket");
   string comment = JsonStr(js, "comment");
   if (comment == "")
      comment = "bridge";

   bool ok = false;
   if (op == "open")
   {
      if (type == "sell")
         ok = trade.Sell(volume, symbol, price, sl, tp, comment);
      else
         ok = trade.Buy(volume, symbol, price, sl, tp, comment);
   }
   else if (op == "close")
   {
      ok = trade.PositionClose((ulong)ticket);
   }
   else if (op == "modify")
   {
      ok = trade.PositionModify((ulong)ticket, sl, tp);
   }
   else if (op == "pending")
   {
      ENUM_ORDER_TYPE ot = ORDER_TYPE_BUY_LIMIT;
      if (type == "sell_limit")
         ot = ORDER_TYPE_SELL_LIMIT;
      else if (type == "buy_stop")
         ot = ORDER_TYPE_BUY_STOP;
      else if (type == "sell_stop")
         ot = ORDER_TYPE_SELL_STOP;
      ok = trade.OrderOpen(symbol, ot, volume, 0.0, price, sl, tp, ORDER_TIME_GTC, 0, comment);
   }
   else if (op == "delete")
   {
      ok = trade.OrderDelete((ulong)ticket);
   }
   else if (op == "close_all")
   {
      ok = true;
      for (int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong t = PositionGetTicket(i);
         if (PositionSelectByTicket(t) && PositionGetString(POSITION_SYMBOL) == symbol)
            if (!trade.PositionClose(t))
               ok = false;
      }
   }

   string res = StringFormat(
      "{\"id\":\"%s\",\"ok\":%s,\"retcode\":%u,\"order\":%I64u,\"deal\":%I64u,\"price\":%.5f,\"comment\":\"%s\",\"time\":%d}",
      id, ok ? "true" : "false", trade.ResultRetcode(), trade.ResultOrder(), trade.ResultDeal(),
      trade.ResultPrice(), trade.ResultComment(), (int)TimeLocal());
   WriteText(resFile, res);
}

void WriteState()
{
   string s = StringFormat(
      "{\"time\":%d,\"login\":%I64d,\"server\":\"%s\",\"trade_allowed\":%d,\"balance\":%.2f,\"equity\":%.2f,\"positions\":%d}",
      (int)TimeLocal(), AccountInfoInteger(ACCOUNT_LOGIN), AccountInfoString(ACCOUNT_SERVER),
      (int)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED), AccountInfoDouble(ACCOUNT_BALANCE),
      AccountInfoDouble(ACCOUNT_EQUITY), PositionsTotal());
   WriteText(stateFile, s);
}
