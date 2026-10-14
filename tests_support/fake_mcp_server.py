"""Minimal MCP stdio server for tests: echo + add tools, one tool marked read-only."""
import json, sys
def send(obj):
    sys.stdout.write(json.dumps(obj) + '\n'); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    msg = json.loads(line)
    method, mid, params = msg.get('method'), msg.get('id'), msg.get('params') or {}
    if method == 'initialize':
        send({'jsonrpc': '2.0', 'id': mid, 'result': {'protocolVersion': params.get('protocolVersion'), 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fake', 'version': '1'}}})
    elif method == 'notifications/initialized':
        pass
    elif method == 'tools/list':
        send({'jsonrpc': '2.0', 'id': mid, 'result': {'tools': [
            {'name': 'echo', 'description': 'Echo text back', 'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}, 'annotations': {'readOnlyHint': True}},
            {'name': 'add', 'description': 'Add two numbers', 'inputSchema': {'type': 'object', 'properties': {'a': {'type': 'number'}, 'b': {'type': 'number'}}, 'required': ['a', 'b']}},
            {'name': 'boom', 'description': 'Always errors', 'inputSchema': {'type': 'object', 'properties': {}}}]}})
    elif method == 'tools/call':
        name, args = params.get('name'), params.get('arguments') or {}
        if name == 'echo':
            send({'jsonrpc': '2.0', 'id': mid, 'result': {'content': [{'type': 'text', 'text': 'echo: ' + str(args.get('text'))}]}})
        elif name == 'add':
            send({'jsonrpc': '2.0', 'id': mid, 'result': {'content': [{'type': 'text', 'text': str(args['a'] + args['b'])}]}})
        elif name == 'boom':
            send({'jsonrpc': '2.0', 'id': mid, 'result': {'content': [{'type': 'text', 'text': 'kaboom'}], 'isError': True}})
        else:
            send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': 'unknown tool'}})
    else:
        send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': 'method not found'}})
