#property strict
#property version "1.20"

input string InpPrefix = "bridge";

string g_cmdFile;
string g_resFile;
string g_stateFile;
string g_lastId = "";

int OnInit()
{
   g_cmdFile = InpPrefix + "_cmd.json";
   g_resFile = InpPrefix + "_res.json";
   g_stateFile = InpPrefix + "_state.json";
   EventSetTimer(1);
   WriteState();
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   WriteState();
   ReadCommand();
}

void OnTick()
{
   WriteState();
   ReadCommand();
}

string ReadAll(const string name)
{
   int h = FileOpen(name, FILE_READ | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE)
      return "";
   string out = "";
   while(!FileIsEnding(h))
      out += FileReadString(h) + "\n";
   FileClose(h);
   return out;
}

void WriteAll(const string name, const string body)
{
   int h = FileOpen(name, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE)
      return;
   FileWriteString(h, body);
   FileClose(h);
}

string JStr(const string js, const string key)
{
   string pat = "\"" + key + "\"";
   int p = StringFind(js, pat);
   if(p < 0)
      return "";
   p = StringFind(js, ":", p + StringLen(pat));
   if(p < 0)
      return "";
   p++;
   while(p < StringLen(js))
   {
      ushort c = StringGetCharacter(js, p);
      if(c != ' ' && c != '\t' && c != '\r' && c != '\n')
         break;
      p++;
   }
   if(p >= StringLen(js))
      return "";
   if(StringGetCharacter(js, p) == '"')
   {
      p++;
      string out = "";
      while(p < StringLen(js))
      {
         ushort c = StringGetCharacter(js, p);
         if(c == '"')
            break;
         if(c == '\\' && p + 1 < StringLen(js))
         {
            p++;
            c = StringGetCharacter(js, p);
            if(c == 'n')
               out += "\n";
            else if(c == 't')
               out += "\t";
            else
               out += ShortToString(c);
         }
         else
            out += ShortToString(c);
         p++;
      }
      return out;
   }
   int e = p;
   while(e < StringLen(js))
   {
      ushort c = StringGetCharacter(js, e);
      if(c == ',' || c == '}' || c == ']' || c == ' ' || c == '\r' || c == '\n' || c == '\t')
         break;
      e++;
   }
   return StringSubstr(js, p, e - p);
}

double JNum(const string js, const string key)
{
   return StringToDouble(JStr(js, key));
}

int JInt(const string js, const string key)
{
   return (int)StringToInteger(JStr(js, key));
}

string Esc(const string s)
{
   string out = "";
   for(int i = 0; i < StringLen(s); i++)
   {
      ushort c = StringGetCharacter(s, i);
      if(c == '"' || c == '\\')
         out += "\\";
      out += ShortToString(c);
   }
   return out;
}

void WriteState()
{
   string pos = "";
   int total = PositionsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(pos != "")
         pos += ",";
      pos += "{\"ticket\":" + (string)ticket
           + ",\"symbol\":\"" + Esc(PositionGetString(POSITION_SYMBOL)) + "\""
           + ",\"type\":" + (string)PositionGetInteger(POSITION_TYPE)
           + ",\"volume\":" + DoubleToString(PositionGetDouble(POSITION_VOLUME), 2)
           + ",\"price\":" + DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN), 5)
           + ",\"sl\":" + DoubleToString(PositionGetDouble(POSITION_SL), 5)
           + ",\"tp\":" + DoubleToString(PositionGetDouble(POSITION_TP), 5)
           + ",\"profit\":" + DoubleToString(PositionGetDouble(POSITION_PROFIT), 2)
           + "}";
   }
   string body = "{\"login\":" + (string)AccountInfoInteger(ACCOUNT_LOGIN)
               + ",\"server\":\"" + Esc(AccountInfoString(ACCOUNT_SERVER)) + "\""
               + ",\"trade_allowed\":" + ((AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) && TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) && MQLInfoInteger(MQL_TRADE_ALLOWED)) ? "true" : "false")
               + ",\"balance\":" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2)
               + ",\"equity\":" + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2)
               + ",\"margin\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 2)
               + ",\"positions\":[" + pos + "]}";
   WriteAll(g_stateFile, body);
}

void WriteResult(const string id, const bool ok, const string comment, const ulong deal, const ulong order, const int retcode)
{
   string body = "{\"id\":\"" + Esc(id) + "\",\"ok\":" + (ok ? "true" : "false")
               + ",\"retcode\":" + (string)retcode
               + ",\"deal\":" + (string)deal
               + ",\"order\":" + (string)order
               + ",\"comment\":\"" + Esc(comment) + "\"}";
   WriteAll(g_resFile, body);
}

int Filling(const string sym)
{
   long mode = SymbolInfoInteger(sym, SYMBOL_FILLING_MODE);
   if((mode & SYMBOL_FILLING_IOC) != 0)
      return ORDER_FILLING_IOC;
   if((mode & SYMBOL_FILLING_FOK) != 0)
      return ORDER_FILLING_FOK;
   return ORDER_FILLING_RETURN;
}

void DoOpen(const string id, const string sym, const string type, const double vol, const double sl, const double tp, const string comment)
{
   MqlTradeRequest req;
   MqlTradeResult res;
   ZeroMemory(req);
   ZeroMemory(res);
   req.action = TRADE_ACTION_DEAL;
   req.symbol = sym;
   req.volume = vol;
   req.sl = sl;
   req.tp = tp;
   req.deviation = 50;
   req.magic = 20261109;
   req.comment = comment;
   req.type_filling = (ENUM_ORDER_TYPE_FILLING)Filling(sym);
   if(type == "sell")
   {
      req.type = ORDER_TYPE_SELL;
      req.price = SymbolInfoDouble(sym, SYMBOL_BID);
   }
   else
   {
      req.type = ORDER_TYPE_BUY;
      req.price = SymbolInfoDouble(sym, SYMBOL_ASK);
   }
   bool ok = OrderSend(req, res);
   WriteResult(id, ok && (res.retcode == TRADE_RETCODE_DONE || res.retcode == TRADE_RETCODE_PLACED), res.comment, res.deal, res.order, res.retcode);
}

void DoPending(const string id, const string sym, const string type, const double vol, const double price, const double sl, const double tp, const string comment)
{
   MqlTradeRequest req;
   MqlTradeResult res;
   ZeroMemory(req);
   ZeroMemory(res);
   req.action = TRADE_ACTION_PENDING;
   req.symbol = sym;
   req.volume = vol;
   req.price = price;
   req.sl = sl;
   req.tp = tp;
   req.deviation = 50;
   req.magic = 20261109;
   req.comment = comment;
   req.type_time = ORDER_TIME_GTC;
   req.type_filling = (ENUM_ORDER_TYPE_FILLING)Filling(sym);
   if(type == "sell_limit")
      req.type = ORDER_TYPE_SELL_LIMIT;
   else if(type == "buy_stop")
      req.type = ORDER_TYPE_BUY_STOP;
   else if(type == "sell_stop")
      req.type = ORDER_TYPE_SELL_STOP;
   else
      req.type = ORDER_TYPE_BUY_LIMIT;
   bool ok = OrderSend(req, res);
   WriteResult(id, ok && (res.retcode == TRADE_RETCODE_DONE || res.retcode == TRADE_RETCODE_PLACED), res.comment, res.deal, res.order, res.retcode);
}

void DoClose(const string id, const ulong ticket, const double vol, const string comment)
{
   if(!PositionSelectByTicket(ticket))
   {
      WriteResult(id, false, "position not found", 0, 0, -1);
      return;
   }
   string sym = PositionGetString(POSITION_SYMBOL);
   long ptype = PositionGetInteger(POSITION_TYPE);
   double pvol = vol > 0 ? vol : PositionGetDouble(POSITION_VOLUME);
   MqlTradeRequest req;
   MqlTradeResult res;
   ZeroMemory(req);
   ZeroMemory(res);
   req.action = TRADE_ACTION_DEAL;
   req.symbol = sym;
   req.position = ticket;
   req.volume = pvol;
   req.deviation = 50;
   req.magic = 20261109;
   req.comment = comment;
   req.type_filling = (ENUM_ORDER_TYPE_FILLING)Filling(sym);
   if(ptype == POSITION_TYPE_BUY)
   {
      req.type = ORDER_TYPE_SELL;
      req.price = SymbolInfoDouble(sym, SYMBOL_BID);
   }
   else
   {
      req.type = ORDER_TYPE_BUY;
      req.price = SymbolInfoDouble(sym, SYMBOL_ASK);
   }
   bool ok = OrderSend(req, res);
   WriteResult(id, ok && (res.retcode == TRADE_RETCODE_DONE || res.retcode == TRADE_RETCODE_PLACED), res.comment, res.deal, res.order, res.retcode);
}

void DoModify(const string id, const ulong ticket, const double sl, const double tp)
{
   MqlTradeRequest req;
   MqlTradeResult res;
   ZeroMemory(req);
   ZeroMemory(res);
   req.action = TRADE_ACTION_SLTP;
   req.position = ticket;
   req.sl = sl;
   req.tp = tp;
   bool ok = OrderSend(req, res);
   WriteResult(id, ok && (res.retcode == TRADE_RETCODE_DONE || res.retcode == TRADE_RETCODE_PLACED), res.comment, res.deal, res.order, res.retcode);
}

void DoDelete(const string id, const ulong ticket)
{
   MqlTradeRequest req;
   MqlTradeResult res;
   ZeroMemory(req);
   ZeroMemory(res);
   req.action = TRADE_ACTION_REMOVE;
   req.order = ticket;
   bool ok = OrderSend(req, res);
   WriteResult(id, ok && (res.retcode == TRADE_RETCODE_DONE || res.retcode == TRADE_RETCODE_PLACED), res.comment, res.deal, res.order, res.retcode);
}

void DoCloseAll(const string id, const string comment)
{
   int total = PositionsTotal();
   int closed = 0;
   for(int i = total - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      DoClose(id, ticket, 0, comment);
      closed++;
   }
   WriteResult(id, true, "closed " + (string)closed, 0, 0, TRADE_RETCODE_DONE);
}

void ReadCommand()
{
   string js = ReadAll(g_cmdFile);
   if(js == "")
      return;
   string id = JStr(js, "id");
   if(id == "" || id == g_lastId)
      return;
   g_lastId = id;
   string op = JStr(js, "op");
   string sym = JStr(js, "symbol");
   string type = JStr(js, "type");
   string comment = JStr(js, "comment");
   double vol = JNum(js, "volume");
   double price = JNum(js, "price");
   double sl = JNum(js, "sl");
   double tp = JNum(js, "tp");
   ulong ticket = (ulong)JNum(js, "ticket");
   if(comment == "")
      comment = "bridge";
   if(op == "open")
      DoOpen(id, sym, type, vol, sl, tp, comment);
   else if(op == "pending")
      DoPending(id, sym, type, vol, price, sl, tp, comment);
   else if(op == "close")
      DoClose(id, ticket, vol, comment);
   else if(op == "modify")
      DoModify(id, ticket, sl, tp);
   else if(op == "delete")
      DoDelete(id, ticket);
   else if(op == "close_all")
      DoCloseAll(id, comment);
   else
      WriteResult(id, false, "unknown op " + op, 0, 0, -2);
   FileDelete(g_cmdFile);
}
