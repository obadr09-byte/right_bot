import sys
import os
import pathlib
import re
import io
from datetime import datetime
from supabase import create_client, Client
from weasyprint import HTML
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from typing import cast

# ----------------------------------------------------
# (1) ⚠️ تحميل الأسرار من ملف .env
# ----------------------------------------------------
load_dotenv() 

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not all([SUPABASE_URL, SUPABASE_KEY, BOT_TOKEN]):
    print("❌ خطأ فادح: واحد أو أكثر من المتغيرات (URL, KEY, TOKEN) مفقود في ملف .env")
    sys.exit(1)

# ----------------------------------------------------
# (2) ⚙️ إعداد الاتصال بـ Supabase
# ----------------------------------------------------
try:
    supabase: Client = create_client(
        cast(str, SUPABASE_URL),
        cast(str, SUPABASE_KEY)
    )
    print("✅ تم الاتصال بـ Supabase بنجاح.")
except Exception as e:
    print(f"❌ خطأ في الاتصال بـ Supabase: {e}")
    sys.exit(1)

# ----------------------------------------------------
# (3) 📄 دالة ملء القالب
# ----------------------------------------------------
def _populate_single_invoice_html(template: str, invoice_data: dict) -> str:
    """يملأ قالب الفاتورة الواحدة (الملصق + الصندوق) بالبيانات"""
    
    populated_html = template
    
    # ------------------------------
    # بناء صفوف الأصناف
    # ------------------------------
    items_rows_html = ""
    if isinstance(invoice_data.get("items_data"), list):
        for item in invoice_data["items_data"]:
            if not isinstance(item, dict):
                continue
            
            item_name = item.get('product_name', 'N/A')
            item_details = item.get('details', 'N/A')
            item_sub_total = float(item.get('sub_total', 0))
            
            items_rows_html += f"""
            <tr>
                <td>{item_name}</td>
                <td>{item_details}</td>
                <td>{item_sub_total:.2f} ج</td>
            </tr>
            """
    
    # ------------------------------
    # بيانات العميل والمجموعات
    # ------------------------------
    customer_data = invoice_data.get("customer_data") if isinstance(invoice_data.get("customer_data"), dict) else {}
    totals_data = invoice_data.get("totals_data") if isinstance(invoice_data.get("totals_data"), dict) else {}
    invoice_id = invoice_data.get("invoice_id", "N/A")
    
    invoice_date_str = str((customer_data or {}).get("date", ""))
    try:
        invoice_date = datetime.fromisoformat(invoice_date_str.replace('Z', '+00:00'))
        invoice_date_str = invoice_date.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        pass 

    populated_html = populated_html.replace("{{invoice_id}}", str(invoice_id))
    populated_html = populated_html.replace("{{invoice_date}}", invoice_date_str)
    populated_html = populated_html.replace("{{customer_name}}", str((customer_data or {}).get("name", "N/A")))
    populated_html = populated_html.replace("{{customer_phone}}", str((customer_data or {}).get("phone", "") or "غير مسجل"))
    populated_html = populated_html.replace("{{customer_address}}", str((customer_data or {}).get("address", "") or "غير مسجل"))

    # ------------------------------
    # شعار الشركة
    # ------------------------------
    logo_path = os.path.join(os.path.dirname(__file__), "logo.png")
    if os.path.exists(logo_path):
        logo_path_url = pathlib.Path(logo_path).resolve().as_uri()
        populated_html = populated_html.replace("logo.png", logo_path_url)
    else:
        populated_html = populated_html.replace('<img src="logo.png" alt="Logo" class="logo">', '<h2>شعار الشركة</h2>')

    # ------------------------------
    # المبالغ المالية
    # ------------------------------
    sub_total = float((totals_data or {}).get("sub_total", 0) or 0)
    populated_html = populated_html.replace("{{sub_total}}", f"{sub_total:.2f} ج")
    
    discount_amount = float((totals_data or {}).get('discount_amount', 0) or 0)
    shipping_cost = float((totals_data or {}).get('shipping_cost', 0) or 0)
    discount_html = ""
    
    if discount_amount > 0:
        discount_html += f'<tr><td>خصم:</td><td class="total">- {discount_amount:.2f} ج</td></tr>'
    if shipping_cost > 0:
        discount_html += f'<tr><td>مصاريف الشحن:</td><td class="total">+ {shipping_cost:.2f} ج</td></tr>'
    
    populated_html = populated_html.replace("{{discount_section}}", discount_html)
    final_total = float((totals_data or {}).get('final_total', 0) or 0)
    populated_html = populated_html.replace("{{final_total}}", f"{final_total:.2f} ج")

    # ------------------------------
    # استبدال صفوف الأصناف في القالب
    # ------------------------------
    populated_html = populated_html.replace("{{items_rows}}", items_rows_html)
    
    return populated_html

# ----------------------------------------------------
# (4) 🤖 دالة جلب بيانات الفاتورة
# ----------------------------------------------------
async def get_invoice_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_message = update.message.text
    chat_id = update.message.chat_id
    print(f"\nIncoming request from chat_id {chat_id}: {user_message}")

    try:
        invoice_id = int(context.args[0])
    except (IndexError, ValueError, TypeError):
        await update.message.reply_text("❌ خطأ: الرقم المرسل غير صحيح.")
        return

    await update.message.reply_text(f"... جاري البحث عن الفاتورة رقم {invoice_id} ...")

    try:
        invoices_resp = supabase.table('invoices').select("*").eq('invoice_id', invoice_id).single().execute()
        items_resp = supabase.table('invoice_items').select("*").eq('invoice_id', invoice_id).execute()
        
        inv_data = invoices_resp.data
        items_data = items_resp.data

        if not inv_data or not isinstance(inv_data, dict):
            await update.message.reply_text(f"❌ لم يتم العثور على فاتورة بهذا الرقم: {invoice_id}")
            return
        
        customer_name = inv_data.get('customer_name', 'N/A')
        customer_phone = inv_data.get('customer_phone', 'N/A')
        customer_address = inv_data.get('customer_address', 'N/A')
        status = inv_data.get('status', 'N/A')
        
        def safe_float(val):
            try:
                if isinstance(val, (int, float)):
                    return float(val)
                elif isinstance(val, str) and val.replace('.', '', 1).isdigit():
                    return float(val)
            except Exception:
                pass
            return 0.0

        sub_total = safe_float(inv_data.get('sub_total', 0))
        shipping_cost = safe_float(inv_data.get('shipping_cost', 0))
        discount_amount = safe_float(inv_data.get('discount_amount', 0))
        final_total = safe_float(inv_data.get('final_total', 0))

        message_parts = [
            f"🧾 **تفاصيل الفاتورة رقم: {invoice_id}** 🧾",
            f"----------------------------------------",
            f"▪️ *العميل:* {customer_name}",
            f"▪️ *الهاتف:* {customer_phone}",
            f"▪️ *العنوان:* {customer_address}",
            f"▪️ *الحالة الحالية:* {status}",
            f"----------------------------------------",
            f"📋 *تفاصيل الأوردر:*"
        ]

        if isinstance(items_data, list) and items_data:
            items_to_show = items_data[:20]
            for item in items_to_show:
                if isinstance(item, dict):
                    name = item.get('product_name', 'صنف')
                    details = item.get('details', '...')
                    item_total = safe_float(item.get('sub_total', 0))
                    message_parts.append(f"  - {name} ({details}) = *{item_total:.2f} ج*")
            if len(items_data) > 20:
                message_parts.append(f"  (... و {len(items_data) - 20} أصناف أخرى ...)")
        else:
            message_parts.append("  (لا توجد أصناف في هذه الفاتورة)")

        message_parts.extend([
            f"----------------------------------------",
            f"▪️ *إجمالي المنتجات:* {sub_total:,.2f} ج"
        ])
        if discount_amount > 0:
            message_parts.append(f"▪️ *الخصم:* -{discount_amount:,.2f} ج")
        if shipping_cost > 0:
            message_parts.append(f"▪️ *الشحن:* +{shipping_cost:,.2f} ج")
        message_parts.append(f"▪️ *الإجمالي النهائي:* **{final_total:,.2f} ج**")

        detailed_message = "\n".join(message_parts)
        if len(detailed_message) > 4096:
            detailed_message = detailed_message[:4090] + "\n... (تم قص الرسالة للطول)"

        await update.message.reply_text(detailed_message, parse_mode="Markdown")

        await update.message.reply_text("... جاري تحضير ملف الـ PDF ...")
        
        invoice_data_dict = {
            "invoice_id": invoice_id,
            "customer_data": {
                "name": customer_name,
                "phone": customer_phone,
                "address": customer_address,
                "date": inv_data.get('invoice_date')
            },
            "totals_data": {
                "sub_total": sub_total,
                "discount_amount": discount_amount,
                "shipping_cost": shipping_cost,
                "final_total": final_total
            },
            "items_data": items_data
        }
        
        template_path = os.path.join(os.path.dirname(__file__), "template.html")
        with open(template_path, 'r', encoding='utf-8') as f:
            html_template_full = f.read()

        body_match = re.search(r'<body>(.*)</body>', html_template_full, re.DOTALL)
        style_match = re.search(r'<style>(.*?)</style>', html_template_full, re.DOTALL)

        if not body_match or not style_match:
            await update.message.reply_text("❌ لا يمكن قراءة <body> أو <style> من template.html")
            return
            
        body_template = body_match.group(1)
        style_content = style_match.group(1)
        
        populated_html_body = _populate_single_invoice_html(body_template, invoice_data_dict)
        
        final_html = f"""
        <!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
        <style>{style_content}</style>
        </head><body>{populated_html_body}</body></html>
        """
        
        pdf_bytes = io.BytesIO()
        base_url = pathlib.Path(os.path.dirname(os.path.abspath(__file__))).resolve().as_uri()
        HTML(string=final_html, base_url=base_url).write_pdf(pdf_bytes)
        pdf_bytes.seek(0)

        await update.message.reply_document(
            document=pdf_bytes,
            filename=f"invoice_{invoice_id}.pdf",
            caption=f"📄 تفضل ملف PDF للفاتورة رقم {invoice_id}"
        )

    except Exception as e:
        print(f"❌ خطأ أثناء معالجة الفاتورة {invoice_id}: {e}")
        await update.message.reply_text("❌ حدث خطأ فني أثناء جلب بيانات الفاتورة. يرجى المحاولة لاحقاً.")

# ----------------------------------------------------
# (5) دالة لمعالجة أي رسالة رقمية
# ----------------------------------------------------
async def handle_invoice_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_text = update.message.text.strip()
    if not user_text.isdigit():
        return

    context.args = [user_text]
    await get_invoice_info(update, context)

# ----------------------------------------------------
# (6) الدالة الرئيسية لتشغيل البوت
# ----------------------------------------------------
def main():
    print("Starting bot...")

    application = Application.builder().token(BOT_TOKEN).build()

    # أي رسالة نصية غير أمر تتحول لدالة معالجة الأرقام
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_invoice_number))

    print("✅ البوت جاهز ويعمل (Ctrl+C للإيقاف)")
    application.run_polling()

if __name__ == "__main__":
    main()
