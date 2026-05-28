import json
import os
from dotenv import load_dotenv
from larksuiteoapi import Config, LogLevel
from larksuiteoapi.event import EventDispatcher, WSClient
from larksuiteoapi.service.drive.v1 import DriveService

# 加载环境变量（推荐使用.env文件管理敏感信息）
load_dotenv()

# -------------------------- 配置区域（请修改为你自己的信息） --------------------------
APP_ID = os.getenv("FEISHU_APP_ID", "你的AppID")
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "你的AppSecret")
# -----------------------------------------------------------------------------------

def parse_field_value(field_value_str):
    """解析飞书字段值（JSON字符串转Python对象）"""
    try:
        return json.loads(field_value_str)
    except json.JSONDecodeError:
        return field_value_str

def handle_bitable_record_changed(ctx):
    """处理多维表格记录变更事件"""
    print("\n" + "="*80)
    print("✅ 收到多维表格记录变更事件")
    print("="*80)
    
    # 打印事件头部信息
    header = ctx.header
    print(f"📌 事件ID: {header.event_id}")
    print(f"📅 事件时间: {header.create_time}")
    print(f"🏢 企业租户: {header.tenant_key}")
    print(f"🔗 事件类型: {header.event_type}")
    
    # 打印事件业务数据
    event = ctx.event
    print(f"\n📄 多维表格ID: {event.file_token}")
    print(f"📊 数据表ID: {event.table_id}")
    print(f"🔢 表格版本号: {event.revision}")
    
    # 打印操作人信息
    operator = event.operator_id
    print(f"\n👤 操作人OpenID: {operator.open_id}")
    if hasattr(operator, 'user_id') and operator.user_id:
        print(f"👤 操作人UserID: {operator.user_id}")
    if hasattr(operator, 'union_id') and operator.union_id:
        print(f"👤 操作人UnionID: {operator.union_id}")
    
    # 遍历所有变更操作
    print("\n📝 变更详情:")
    print("-"*60)
    
    for action in event.action_list:
        action_type = action.action
        record_id = action.record_id
        
        print(f"\n🔹 操作类型: {action_type}")
        print(f"🔹 记录ID: {record_id}")
        
        if action_type == "record_added":
            print("\n📥 新增字段值:")
            for field in action.after_value:
                field_id = field.field_id
                field_value = parse_field_value(field.field_value)
                print(f"  - {field_id}: {field_value}")
                
        elif action_type == "record_edited":
            print("\n📝 修改前字段值:")
            for field in action.before_value:
                field_id = field.field_id
                field_value = parse_field_value(field.field_value)
                print(f"  - {field_id}: {field_value}")
                
            print("\n📝 修改后字段值:")
            for field in action.after_value:
                field_id = field.field_id
                field_value = parse_field_value(field.field_value)
                print(f"  - {field_id}: {field_value}")
                
        elif action_type == "record_deleted":
            print("\n🗑️ 删除前字段值:")
            for field in action.before_value:
                field_id = field.field_id
                field_value = parse_field_value(field.field_value)
                print(f"  - {field_id}: {field_value}")
    
    print("\n" + "="*80 + "\n")

def on_connect():
    """长连接成功建立回调"""
    print("\n" + "🎉"*20)
    print("✅ 飞书长连接已成功建立！")
    print("📢 现在可以在多维表格中进行操作，事件将在这里打印")
    print("🎉"*20 + "\n")

def on_disconnect(code, reason):
    """长连接断开回调"""
    print(f"\n❌ 飞书长连接已断开")
    print(f"断开代码: {code}")
    print(f"断开原因: {reason}")
    print("🔄 SDK将自动进行指数退避重连...\n")

def on_error(err):
    """长连接错误回调"""
    print(f"\n⚠️ 飞书长连接发生错误: {err}\n")

def main():
    # 创建应用配置
    config = Config.new_internal_app_config(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        log_level=LogLevel.DEBUG,  # 开启调试日志，生产环境可改为INFO
    )
    
    # 创建事件分发器
    event_dispatcher = EventDispatcher()
    
    # 注册多维表格记录变更事件处理器
    event_dispatcher.register(
        DriveService.EVENT_DRIVE_FILE_BITABLE_RECORD_CHANGED_V1,
        handle_bitable_record_changed
    )
    
    # 可选：注册多维表格字段变更事件处理器
    # event_dispatcher.register(
    #     DriveService.EVENT_DRIVE_FILE_BITABLE_FIELD_CHANGED_V1,
    #     handle_bitable_field_changed
    # )
    
    # 创建长连接客户端
    ws_client = WSClient(config, event_dispatcher)
    
    # 注册连接状态回调
    ws_client.on("connect", on_connect)
    ws_client.on("disconnect", on_disconnect)
    ws_client.on("error", on_error)
    
    # 启动长连接（阻塞运行）
    print("🚀 正在启动飞书长连接客户端...")
    print(f"📱 应用ID: {APP_ID}")
    print("🔍 请确保已将应用添加为目标多维表格的可编辑协作者")
    print("🔍 请确保已在飞书开放平台发布应用并生效权限\n")
    
    ws_client.start()

if __name__ == "__main__":
    main()