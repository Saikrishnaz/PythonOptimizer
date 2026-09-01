"""
script_analyzer.py
------------------
AST-based CONFIG parameter auto-detection for any Python backtest script.

Scans for:
1. CONFIG = { ... }  dict literal at module level or in if __name__
2. CONFIG = dict(...)  call expression
3. StrategyConfig.__init__ parameters (class-based configs)
4. def main(...)  keyword arguments with defaults

Returns a structured JSON schema the frontend renders as a parameter form.
"""

import ast
import os
import json
import re
import inspect
import importlib.util
from typing import Any


def _infer_type(value: Any) -> str:
    """Infer the parameter type string from a Python value."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        # Check if it looks like a file path
        if os.sep in value or '/' in value or '\\' in value:
            if any(value.lower().endswith(ext) for ext in ['.csv', '.parquet', '.xlsx', '.xls', '.json', '.txt']):
                return "path"
        # Check if it looks like a date
        if re.match(r'^\d{4}-\d{2}-\d{2}', value):
            return "date_str"
        # Check if it looks like a time
        if re.match(r'^\d{2}:\d{2}(:\d{2})?$', value):
            return "time_str"
        return "str"
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, list):
        return "list"
    if isinstance(value, tuple):
        return "list"
    if value is None:
        return "str"
    return "str"


def _classify_param(name: str, param_type: str) -> str:
    """Classify parameter as 'fixed' (non-optimizable) or 'optimizable'."""
    # Paths and file references are always fixed
    if param_type in ("path",):
        return "fixed"
    # Dict/list types are usually structural, not optimizable
    if param_type in ("dict", "list"):
        return "fixed"
    # Known fixed parameter name patterns
    fixed_patterns = [
        'path', 'dir', 'file', 'csv', 'parquet', 'xlsx', 'report',
        'symbol', 'show_pnl', 'display', 'debug'
    ]
    name_lower = name.lower()
    for pattern in fixed_patterns:
        if pattern in name_lower:
            return "fixed"
    # Numeric and bool types are typically optimizable
    if param_type in ("int", "float", "bool"):
        return "optimizable"
    # String types that look like choices
    if param_type == "str":
        return "optimizable"
    return "fixed"


def _safe_eval_node(node: ast.AST) -> Any:
    """Safely evaluate an AST node to extract a Python value."""
    try:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            val = _safe_eval_node(node.operand)
            if val is not None:
                return -val
        if isinstance(node, ast.Dict):
            keys = [_safe_eval_node(k) for k in node.keys]
            values = [_safe_eval_node(v) for v in node.values]
            if None not in keys:
                return dict(zip(keys, values))
        if isinstance(node, ast.List):
            return [_safe_eval_node(el) for el in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(_safe_eval_node(el) for el in node.elts)
        if isinstance(node, ast.Call):
            # Handle datetime.time(h, m), datetime.date(y, m, d), etc.
            func_name = _get_call_name(node)
            if func_name in ('dtime', 'time', 'datetime.time'):
                args = [_safe_eval_node(a) for a in node.args]
                if all(a is not None for a in args):
                    return f"{int(args[0]):02d}:{int(args[1]):02d}:{int(args[2]):02d}" if len(args) > 2 else f"{int(args[0]):02d}:{int(args[1]):02d}:00"
            if func_name in ('ddate', 'date', 'datetime.date'):
                args = [_safe_eval_node(a) for a in node.args]
                if all(a is not None for a in args):
                    return f"{int(args[0])}-{int(args[1]):02d}-{int(args[2]):02d}"
            if func_name == 'dict':
                result = {}
                for kw in node.keywords:
                    if kw.arg is not None:
                        result[kw.arg] = _safe_eval_node(kw.value)
                return result
        if isinstance(node, ast.Attribute):
            return None  # Can't safely evaluate
        if isinstance(node, ast.Name):
            # Handle True/False/None
            if node.id == 'True':
                return True
            if node.id == 'False':
                return False
            if node.id == 'None':
                return None
        # JoinedStr (f-string), BinOp (string concat), etc. — can't evaluate
        return None
    except Exception:
        return None


def _get_call_name(node: ast.Call) -> str:
    """Get the function name from a Call node."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        if isinstance(node.func.value, ast.Name):
            return f"{node.func.value.id}.{node.func.attr}"
        return node.func.attr
    return ""


def _extract_dict_params(node: ast.Dict) -> list:
    """Extract parameters from a dict literal node."""
    params = []
    for key_node, val_node in zip(node.keys, node.values):
        key = _safe_eval_node(key_node)
        if key is None or not isinstance(key, str):
            continue
        value = _safe_eval_node(val_node)
        param_type = _infer_type(value)
        
        # Serialize value for JSON
        serialized_value = value
        if isinstance(value, (dict, list, tuple)):
            serialized_value = value
        
        params.append({
            "name": key,
            "value": serialized_value,
            "type": param_type,
            "category": _classify_param(key, param_type),
        })
    return params


def _extract_call_params(node: ast.Call) -> list:
    """Extract parameters from a dict() or StrategyConfig() call node."""
    params = []
    for kw in node.keywords:
        if kw.arg is None:
            continue  # **kwargs expansion
        value = _safe_eval_node(kw.value)
        param_type = _infer_type(value)
        params.append({
            "name": kw.arg,
            "value": value,
            "type": param_type,
            "category": _classify_param(kw.arg, param_type),
        })
    return params


def _extract_function_params(node: ast.FunctionDef) -> list:
    """Extract parameters from a function definition with defaults."""
    params = []
    args = node.args
    
    # Get defaults — they align with the END of the args list
    num_defaults = len(args.defaults)
    num_args = len(args.args)
    
    for i, arg in enumerate(args.args):
        if arg.arg in ('self', 'cls'):
            continue
        
        default_idx = i - (num_args - num_defaults)
        if default_idx >= 0:
            value = _safe_eval_node(args.defaults[default_idx])
        else:
            value = None
        
        param_type = _infer_type(value)
        params.append({
            "name": arg.arg,
            "value": value,
            "type": param_type,
            "category": _classify_param(arg.arg, param_type),
        })
    
    # Also handle keyword-only args
    for i, arg in enumerate(args.kwonlyargs):
        if i < len(args.kw_defaults) and args.kw_defaults[i] is not None:
            value = _safe_eval_node(args.kw_defaults[i])
        else:
            value = None
        param_type = _infer_type(value)
        params.append({
            "name": arg.arg,
            "value": value,
            "type": param_type,
            "category": _classify_param(arg.arg, param_type),
        })
    
    return params


def analyze_script(script_path: str) -> dict:
    """
    Analyze a Python backtest script and extract its CONFIG parameters.
    
    Returns:
    {
        "script_path": "...",
        "script_name": "...",
        "entry_point": "main" | "run_backtest" | "...",
        "entry_style": "function_kwargs" | "config_class" | "config_dict",
        "config_class_name": "StrategyConfig" | null,
        "parameters": [
            {
                "name": "st_length",
                "value": 5,
                "type": "int",
                "category": "optimizable"
            },
            ...
        ]
    }
    """
    if not os.path.exists(script_path):
        raise FileNotFoundError(f"Script not found: {script_path}")
    
    with open(script_path, 'r', encoding='utf-8', errors='ignore') as f:
        source = f.read()
    
    tree = ast.parse(source)
    
    result = {
        "script_path": os.path.abspath(script_path),
        "script_name": os.path.basename(script_path),
        "entry_point": None,
        "entry_style": None,
        "config_class_name": None,
        "parameters": [],
    }
    
    # Strategy 1: Look for CONFIG = {...} or CONFIG = dict(...) at module level
    config_dict_params = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == 'CONFIG':
                    if isinstance(node.value, ast.Dict):
                        config_dict_params = _extract_dict_params(node.value)
                        result["parameters"] = config_dict_params
                        result["entry_style"] = "config_dict"
                    elif isinstance(node.value, ast.Call):
                        func_name = _get_call_name(node.value)
                        if func_name == 'dict':
                            config_dict_params = _extract_call_params(node.value)
                            result["parameters"] = config_dict_params
                            result["entry_style"] = "config_dict"
    
    # Strategy 2: Look for class with __init__ that has typed params (StrategyConfig pattern)
    config_class_name_found = None
    config_class_params = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == '__init__':
                    init_params = _extract_function_params(item)
                    if len(init_params) > 3:  # Must have substantial params
                        config_class_name_found = node.name
                        config_class_params = init_params
    
    # If we found both CONFIG dict and a config class, merge: use CONFIG dict values
    # as overrides on the class defaults (the CONFIG dict is the user's actual config)
    if config_dict_params and config_class_params:
        # The CONFIG dict values take priority; class provides defaults + structure
        config_dict_map = {p["name"]: p for p in config_dict_params}
        merged = []
        seen = set()
        for p in config_dict_params:
            merged.append(p)
            seen.add(p["name"])
        # Add any class params not in the CONFIG dict
        for p in config_class_params:
            if p["name"] not in seen:
                merged.append(p)
        result["parameters"] = merged
        result["config_class_name"] = config_class_name_found
        result["entry_style"] = "config_class"
    elif config_class_params and not config_dict_params:
        result["parameters"] = config_class_params
        result["entry_style"] = "config_class"
        result["config_class_name"] = config_class_name_found
    
    # Strategy 3: Look for def main(...) with keyword args
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'main':
            main_params = _extract_function_params(node)
            if len(main_params) > 0:
                result["entry_point"] = "main"
                if not result["parameters"]:
                    result["parameters"] = main_params
                    result["entry_style"] = "function_kwargs"
    
    # Strategy 4: Look for def run_backtest(...) 
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'run_backtest':
            result["entry_point"] = "run_backtest"
            if result["entry_style"] is None:
                rb_params = _extract_function_params(node)
                if len(rb_params) > 0:
                    result["parameters"] = rb_params
                    result["entry_style"] = "function_kwargs"
    
    # If entry_point not found from main/run_backtest search, check if __name__ block calls something
    if result["entry_point"] is None:
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.If):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        func_name = _get_call_name(sub)
                        if func_name in ('run_backtest', 'main'):
                            result["entry_point"] = func_name
                            break
    
    # Ensure entry_point is set
    if result["entry_point"] is None:
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.FunctionDef) and node.name in ('main', 'run_backtest'):
                result["entry_point"] = node.name
                break
    
    # =================================================================
    # WALK-FORWARD COMPATIBILITY CHECK
    # =================================================================
    wfo_compat = {
        "has_start_date": False,
        "has_end_date": False,
        "has_data_path": False,
        "has_timeframe": False,
        "compatible": False,
        "date_param_style": "flat",    # 'flat' or 'nested'
        "date_param_name": "",          # e.g. 'Backtest_period' for nested
        "data_path_param": "",
        "timeframe_param": "",
        "start_date_param": "",
        "end_date_param": "",
    }

    for p in result["parameters"]:
        name = p["name"]
        name_lower = name.lower()
        ptype = p.get("type", "")
        value = p.get("value")

        # Check for start_date / end_date (flat style)
        if name_lower in ("start_date", "start") and ptype in ("date_str", "str"):
            wfo_compat["has_start_date"] = True
            wfo_compat["start_date_param"] = name
        elif name_lower in ("end_date", "end") and ptype in ("date_str", "str"):
            wfo_compat["has_end_date"] = True
            wfo_compat["end_date_param"] = name

        # Check for nested Backtest_period with start_date/end_date
        if ptype == "dict" and isinstance(value, dict):
            if "start_date" in value and "end_date" in value:
                wfo_compat["has_start_date"] = True
                wfo_compat["has_end_date"] = True
                wfo_compat["date_param_style"] = "nested"
                wfo_compat["date_param_name"] = name
                wfo_compat["start_date_param"] = f"{name}.start_date"
                wfo_compat["end_date_param"] = f"{name}.end_date"

        # Check for data path (prefer tick/input data, not output/report paths)
        if ptype == "path" or (ptype == "str" and isinstance(value, str) and
                               any(ext in str(value).lower() for ext in ['.csv', '.parquet'])):
            # Must contain a data-related keyword
            if any(kw in name_lower for kw in ['data', 'tick', 'spot', 'input']):
                # Must NOT be an output/report path
                if not any(excl in name_lower for excl in ['report', 'output', 'excel', 'trades', 'validation', 'candle']):
                    wfo_compat["has_data_path"] = True
                    wfo_compat["data_path_param"] = name

        # Check for timeframe
        if name_lower in ("timeframe", "tf", "period", "interval"):
            wfo_compat["has_timeframe"] = True
            wfo_compat["timeframe_param"] = name

    # Overall compatibility
    wfo_compat["compatible"] = (
        wfo_compat["has_start_date"] and
        wfo_compat["has_end_date"]
    )

    result["wfo_compatibility"] = wfo_compat

    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python script_analyzer.py <path_to_backtest_script.py>")
        sys.exit(1)
    
    result = analyze_script(sys.argv[1])
    print(json.dumps(result, indent=2, default=str))
