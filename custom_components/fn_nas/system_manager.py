import logging
import asyncio
import os
from datetime import datetime

_LOGGER = logging.getLogger(__name__)

class SystemManager:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.logger = _LOGGER.getChild("system_manager")
        # 根据Home Assistant的日志级别动态设置
        self.logger.setLevel(logging.DEBUG if _LOGGER.isEnabledFor(logging.DEBUG) else logging.INFO)
        self.debug_enabled = _LOGGER.isEnabledFor(logging.DEBUG)  # 基于HA调试模式
        self.sensors_debug_path = "/config/fn_nas_debug"
        
        # 温度传感器缓存
        self.cpu_temp_cache = {
            "hwmon_id": None,
            "temp_id": None,
            "driver_type": None,
            "label": None
        }
        self.mobo_temp_cache = {
            "hwmon_id": None,
            "temp_id": None,
            "label": None
        }

    def _debug_log(self, message: str):
        """只在调试模式下输出详细日志"""
        if self.debug_enabled:
            self.logger.debug(message)

    def _info_log(self, message: str):
        """重要信息日志"""
        self.logger.info(message)

    def _warning_log(self, message: str):
        """警告日志"""
        self.logger.warning(message)

    def _error_log(self, message: str):
        """错误日志"""
        self.logger.error(message)

    async def get_system_info(self) -> dict:
        """获取系统信息"""
        system_info = {}
        try:
            # 获取原始运行时间（秒数）
            uptime_output = await self.coordinator.run_command("cat /proc/uptime")
            if uptime_output:
                try:
                    uptime_seconds = float(uptime_output.split()[0])
                    system_info["uptime_seconds"] = uptime_seconds
                    system_info["uptime"] = self.format_uptime(uptime_seconds)
                except (ValueError, IndexError):
                    system_info["uptime_seconds"] = 0
                    system_info["uptime"] = "未知"
            else:
                system_info["uptime_seconds"] = 0
                system_info["uptime"] = "未知"

            # 一次性获取CPU和主板温度
            temps = await self.get_temperatures_from_sensors()
            system_info["cpu_temperature"] = temps["cpu"]
            system_info["motherboard_temperature"] = temps["motherboard"]

            mem_info = await self.get_memory_info()
            system_info.update(mem_info)
            vol_info = await self.get_vol_usage()
            system_info["volumes"] = vol_info

            # 获取CPU原始统计（用于计算使用率）
            cpu_stats = await self.get_cpu_usage()
            system_info.update(cpu_stats)

            # 获取网络接口流量统计
            net_stats = await self.get_network_stats()
            system_info.update(net_stats)

            return system_info

        except Exception as e:
            self.logger.error("Error getting system info: %s", str(e))
            return {
                "uptime_seconds": 0,
                "uptime": "未知",
                "cpu_temperature": "未知",
                "motherboard_temperature": "未知",
                "memory_total": "未知",
                "memory_used": "未知",
                "memory_available": "未知",
                "volumes": {},
                "cpu_usage_percent": None,
                "_cpu_stat_total": None,
                "_cpu_stat_active": None,
                "_cpu_stat_idle": None,
                "interfaces": {}
            }

    async def get_temperatures_from_sensors(self) -> dict:
        """一次性获取CPU和主板温度（优先sensors命令，失败则回退sysfs）"""
        cpu_temp = "未知"
        mobo_temp = "未知"

        # 尝试 sensors
        cpu_temp, mobo_temp = await self._try_sensors_command("sensors")
        have_cpu = cpu_temp != "未知"
        have_mobo = mobo_temp != "未知"

        # use_sudo=False 且 sensors 未获取到温度时，尝试显式 sudo sensors
        # （user_sudo=False 意味着 SSH 会话本身是普通用户权限，但可能有 sudo 能力）
        if not have_cpu or not have_mobo:
            if not getattr(self.coordinator, 'use_sudo', True):
                self._info_log("尝试 sudo sensors")
                sudo_cpu, sudo_mobo = await self._try_sensors_command("sudo -n sensors")
                if not have_cpu:
                    cpu_temp = sudo_cpu
                    have_cpu = cpu_temp != "未知"
                if not have_mobo:
                    mobo_temp = sudo_mobo
                    have_mobo = mobo_temp != "未知"

        # sysfs 回退
        if not have_cpu or not have_mobo:
            self._info_log("使用sysfs回退方法获取温度")
            fallback_temps = await self._get_temperatures_from_sysfs()
            if not have_cpu:
                cpu_temp = fallback_temps["cpu"]
                if cpu_temp != "未知":
                    self._info_log(f"通过sysfs获取CPU温度成功: {cpu_temp}")
            if not have_mobo:
                mobo_temp = fallback_temps["motherboard"]
                if mobo_temp != "未知":
                    self._info_log(f"通过sysfs获取主板温度成功: {mobo_temp}")

        return {"cpu": cpu_temp, "motherboard": mobo_temp}

    async def _try_sensors_command(self, command: str) -> tuple[str, str]:
        """执行sensors命令并解析温度，返回 (cpu_temp, mobo_temp)"""
        cpu_temp = "未知"
        mobo_temp = "未知"
        try:
            self._debug_log(f"执行sensors命令获取温度: {command}")
            sensors_output = await self.coordinator.run_command(command)
            if self.debug_enabled:
                self._debug_log(f"sensors命令输出长度: {len(sensors_output) if sensors_output else 0}")

            if sensors_output:
                cpu_temp = self.extract_cpu_temp_from_sensors(sensors_output)
                mobo_temp = self.extract_mobo_temp_from_sensors(sensors_output)
            else:
                self._warning_log(f"{command} 命令无输出")
        except Exception as e:
            self._warning_log(f"执行 {command} 失败: {e}")

        return cpu_temp, mobo_temp

    async def _get_temperatures_from_sysfs(self) -> dict:
        """通过sysfs直接读取温度（不需要root权限或lm-sensors）"""
        cpu_temp = "未知"
        mobo_temp = "未知"
        discovered = {"cpu": [], "mobo": []}
        unlabeled = []  # 无法判断类型的传感器

        # 方法1: 通过 /sys/class/thermal/thermal_zone* 读取
        try:
            thermal_zones = await self.coordinator.run_command(
                "ls -d /sys/class/thermal/thermal_zone* 2>/dev/null || true"
            )
            if thermal_zones:
                for zone_path in thermal_zones.splitlines():
                    zone_path = zone_path.strip()
                    if not zone_path:
                        continue
                    zone_num = zone_path.rstrip('/').split('thermal_zone')[-1]
                    type_str = await self.coordinator.run_command(
                        f"cat {zone_path}/type 2>/dev/null || true"
                    )
                    temp_raw = await self.coordinator.run_command(
                        f"cat {zone_path}/temp 2>/dev/null || true"
                    )
                    if not temp_raw or not type_str:
                        continue
                    try:
                        temp_c = float(temp_raw.strip()) / 1000.0
                    except ValueError:
                        continue
                    type_str = type_str.strip().lower()
                    # 放宽温度范围，排除明显异常值即可
                    if not (0 <= temp_c <= 120):
                        continue

                    if any(k in type_str for k in [
                        "x86_pkg", "x86", "cpu", "core", "pkg",
                        "soc", "k10temp", "zen", "ddr", "tjmax"
                    ]):
                        discovered["cpu"].append(temp_c)
                        self._debug_log(f"thermal_zone{zone_num} type={type_str} -> CPU候选 {temp_c}°C")
                    elif any(k in type_str for k in ["acpi", "acpitz", "pch", "chipset"]):
                        discovered["mobo"].append(temp_c)
                        self._debug_log(f"thermal_zone{zone_num} type={type_str} -> 主板候选 {temp_c}°C")
                    else:
                        # 未知类型，记下来稍后启发式判断
                        self._debug_log(f"thermal_zone{zone_num} type={type_str} -> 未分类 {temp_c}°C")
                        if temp_c > 0:
                            unlabeled.append(("thermal", temp_c, type_str))
        except Exception as e:
            self._debug_log(f"thermal_zone读取失败: {e}")

        # 方法2: 通过 /sys/class/hwmon 读取（更详细的标签信息）
        try:
            hwmon_dirs = await self.coordinator.run_command(
                "ls -d /sys/class/hwmon/hwmon* 2>/dev/null || true"
            )
            if hwmon_dirs:
                for hwmon_path in hwmon_dirs.splitlines():
                    hwmon_path = hwmon_path.strip()
                    if not hwmon_path:
                        continue
                    hwmon_name = await self.coordinator.run_command(
                        f"cat {hwmon_path}/name 2>/dev/null || true"
                    )
                    hwmon_name = hwmon_name.strip().lower() if hwmon_name else ""

                    # 枚举此hwmon下的所有温度输入
                    temp_inputs = await self.coordinator.run_command(
                        f"ls {hwmon_path}/temp*_input 2>/dev/null || true"
                    )
                    if not temp_inputs:
                        continue
                    for inp_path in temp_inputs.splitlines():
                        inp_path = inp_path.strip()
                        if not inp_path:
                            continue
                        # 提取 temp_id (如 "1" 从 "temp1_input")
                        temp_id = inp_path.split('/')[-1].replace('temp', '').replace('_input', '')
                        label = await self.coordinator.run_command(
                            f"cat {hwmon_path}/temp{temp_id}_label 2>/dev/null || true"
                        )
                        label = label.strip().lower() if label else ""
                        temp_raw = await self.coordinator.run_command(
                            f"cat {inp_path} 2>/dev/null || true"
                        )
                        if not temp_raw:
                            continue
                        try:
                            temp_c = float(temp_raw.strip()) / 1000.0
                        except ValueError:
                            continue
                        if not (0 <= temp_c <= 120):
                            continue

                        # 判断是CPU还是主板 - 扩展关键词
                        cpu_keywords = [
                            "cpu", "core", "package", "tctl", "tdie",
                            "k10temp", "zen", "tjmax", "ccd", "iod",
                            "soc", "vcore", "gt_core"
                        ]
                        mobo_keywords = [
                            "systin", "system", "mb", "mobo", "motherboard",
                            "temp1", "pch", "chipset", "board", "platform",
                            "acpi", "acpitz", "sio", "superio", "it87",
                            "nct", "w83627", "f71868", "f71869",
                        ]
                        # hwmon 驱动名也可用于判断
                        cpu_hwmon_drivers = [
                            "coretemp", "k10temp", "k8temp", "acpitz"
                        ]

                        is_cpu = any(k in label for k in cpu_keywords) or \
                                 any(k in hwmon_name for k in cpu_keywords) or \
                                 any(k in hwmon_name for k in cpu_hwmon_drivers)
                        is_mobo = any(k in label for k in mobo_keywords) or \
                                  any(k in hwmon_name for k in mobo_keywords)

                        if is_cpu:
                            discovered["cpu"].append(temp_c)
                            self._debug_log(f"hwmon {hwmon_name} temp{temp_id} label={label} -> CPU候选 {temp_c}°C")
                        elif is_mobo:
                            discovered["mobo"].append(temp_c)
                            self._debug_log(f"hwmon {hwmon_name} temp{temp_id} label={label} -> 主板候选 {temp_c}°C")
                        else:
                            self._debug_log(f"hwmon {hwmon_name} temp{temp_id} label={label} -> 未分类 {temp_c}°C")
                            if temp_c > 0:
                                unlabeled.append(("hwmon", temp_c, f"{hwmon_name}/{label}"))
        except Exception as e:
            self._debug_log(f"hwmon读取失败: {e}")

        # 启发式：如果未分类的传感器中有合理值，按场景分配
        if unlabeled and (not discovered["cpu"] or not discovered["mobo"]):
            # 取所有未分类温度的中位数
            temps = sorted([t[1] for t in unlabeled])
            if temps:
                if not discovered["cpu"]:
                    # 最高温通常来自CPU（除非有GPU）
                    discovered["cpu"].append(temps[-1])
                    self._debug_log(f"启发式将最高未分类温度 {temps[-1]}°C 分配给CPU")
                if not discovered["mobo"] and len(temps) > 1:
                    # 次高或中位温分配给主板
                    mid = temps[len(temps)//2 - 1] if len(temps) > 2 else temps[0]
                    discovered["mobo"].append(mid)
                    self._debug_log(f"启发式将未分类温度 {mid}°C 分配给主板")

        # 从候选中取最合理值（CPU取最高，主板取中位数）
        if discovered["cpu"]:
            cpu_temp = f"{max(discovered['cpu']):.1f} °C"
        if discovered["mobo"]:
            sorted_mobo = sorted(discovered["mobo"])
            mobo_temp = f"{sorted_mobo[len(sorted_mobo)//2]:.1f} °C"

        return {"cpu": cpu_temp, "motherboard": mobo_temp}

    async def get_cpu_temp_from_kernel(self) -> str:
        """获取CPU温度 - 向后兼容"""
        temps = await self.get_temperatures_from_sensors()
        return temps["cpu"]

    async def get_mobo_temp_from_kernel(self) -> str:
        """获取主板温度 - 向后兼容"""
        temps = await self.get_temperatures_from_sensors()
        return temps["motherboard"]

    async def get_cpu_temp_from_sensors(self) -> str:
        """使用sensors命令获取CPU温度 - 向后兼容"""
        temps = await self.get_temperatures_from_sensors()
        return temps["cpu"]

    async def get_mobo_temp_from_sensors(self) -> str:
        """使用sensors命令获取主板温度 - 向后兼容"""
        temps = await self.get_temperatures_from_sensors()
        return temps["motherboard"]

    def extract_cpu_temp_from_sensors(self, sensors_output: str) -> str:
        """从sensors输出中提取CPU温度"""
        try:
            lines = sensors_output.split('\n')
            self._debug_log(f"解析sensors输出，共{len(lines)}行")
            
            for i, line in enumerate(lines):
                line_lower = line.lower().strip()
                if self.debug_enabled:
                    self._debug_log(f"第{i+1}行: {line_lower}")
                
                # AMD CPU温度关键词
                if any(keyword in line_lower for keyword in [
                    "tctl", "tdie", "k10temp"
                ]):
                    self._debug_log(f"找到AMD CPU温度行: {line}")
                    if '+' in line and '°c' in line_lower:
                        try:
                            temp_match = line.split('+')[1].split('°')[0].strip()
                            temp = float(temp_match)
                            if 0 < temp < 150:
                                self._info_log(f"从sensors提取AMD CPU温度: {temp:.1f}°C")
                                return f"{temp:.1f} °C"
                        except (ValueError, IndexError) as e:
                            self._debug_log(f"解析AMD温度失败: {e}")
                            continue
                
                # Intel CPU温度关键词
                if any(keyword in line_lower for keyword in [
                    "package id", "core 0", "coretemp"
                ]) and not any(exclude in line_lower for exclude in ["fan"]):
                    self._debug_log(f"找到Intel CPU温度行: {line}")
                    if '+' in line and '°c' in line_lower:
                        try:
                            temp_match = line.split('+')[1].split('°')[0].strip()
                            temp = float(temp_match)
                            if 0 < temp < 150:
                                self._info_log(f"从sensors提取Intel CPU温度: {temp:.1f}°C")
                                return f"{temp:.1f} °C"
                        except (ValueError, IndexError) as e:
                            self._debug_log(f"解析Intel温度失败: {e}")
                            continue
                
                # 通用CPU温度模式
                if ('cpu' in line_lower or 'processor' in line_lower) and '+' in line and '°c' in line_lower:
                    self._debug_log(f"找到通用CPU温度行: {line}")
                    try:
                        temp_match = line.split('+')[1].split('°')[0].strip()
                        temp = float(temp_match)
                        if 0 < temp < 150:
                            self._info_log(f"从sensors提取通用CPU温度: {temp:.1f}°C")
                            return f"{temp:.1f} °C"
                    except (ValueError, IndexError) as e:
                        self._debug_log(f"解析通用CPU温度失败: {e}")
                        continue
            
            self._warning_log("未在sensors输出中找到CPU温度")
            return "未知"
            
        except Exception as e:
            self._error_log(f"解析sensors CPU温度输出失败: {e}")
            return "未知"

    def extract_mobo_temp_from_sensors(self, sensors_output: str) -> str:
        """从sensors输出中提取主板温度"""
        try:
            lines = sensors_output.split('\n')
            self._debug_log(f"解析主板温度，共{len(lines)}行")
            
            for i, line in enumerate(lines):
                line_lower = line.lower().strip()
                
                # 主板温度关键词 - 扩展关键词列表
                if any(keyword in line_lower for keyword in [
                    "motherboard", "mobo", "mb", "system", "chipset", 
                    "ambient", "temp1:", "temp2:", "temp3:", "systin",
                    "acpitz", "thermal", "pch", "platform", "board",
                    "sys", "thermal zone", "acpi", "isa"
                ]) and not any(cpu_keyword in line_lower for cpu_keyword in [
                    "cpu", "core", "package", "processor", "tctl", "tdie"
                ]) and not any(exclude in line_lower for exclude in ["fan", "rpm"]):
                    
                    self._debug_log(f"找到可能的主板温度行: {line}")
                    
                    # 多种温度格式匹配
                    temp_value = None
                    
                    # 格式1: +45.0°C (high = +80.0°C, crit = +95.0°C)
                    if '+' in line and '°c' in line_lower:
                        try:
                            temp_match = line.split('+')[1].split('°')[0].strip()
                            temp_value = float(temp_match)
                        except (ValueError, IndexError):
                            pass
                    
                    # 格式2: 45.0°C
                    if temp_value is None and '°c' in line_lower:
                        try:
                            # 查找数字后跟°C的模式
                            import re
                            temp_match = re.search(r'(\d+\.?\d*)\s*°c', line_lower)
                            if temp_match:
                                temp_value = float(temp_match.group(1))
                        except (ValueError, AttributeError):
                            pass
                    
                    # 格式3: 45.0 C (没有°符号)
                    if temp_value is None and (' c' in line_lower or 'c ' in line_lower):
                        try:
                            # 查找数字后跟C的模式
                            import re
                            temp_match = re.search(r'(\d+\.?\d*)\s*c', line_lower)
                            if temp_match:
                                temp_value = float(temp_match.group(1))
                        except (ValueError, AttributeError):
                            pass
                    
                    if temp_value is not None:
                        # 主板温度通常在15-70度之间，但放宽范围到10-80度
                        if 10 <= temp_value <= 80:
                            # 存储候选值，不立即返回
                            import re
                            if not hasattr(self, '_temp_candidates'):
                                self._temp_candidates = []
                            self._temp_candidates.append((temp_value, line))
                            self._debug_log(f"找到有效主板温度候选: {temp_value:.1f}°C")
                        else:
                            self._debug_log(f"主板温度值超出合理范围: {temp_value:.1f}°C")
                        continue
            
            # 处理候选值
            if hasattr(self, '_temp_candidates') and self._temp_candidates:
                # 优先选择温度在25-45度之间的值（典型主板温度）
                ideal_candidates = [t for t in self._temp_candidates if 25 <= t[0] <= 45]
                if ideal_candidates:
                    best_temp = ideal_candidates[0][0]  # 取第一个理想候选值
                else:
                    # 如果没有理想值，取第一个候选值
                    best_temp = self._temp_candidates[0][0]
                
                self._info_log(f"从sensors提取主板温度: {best_temp:.1f}°C")
                # 清理候选值
                delattr(self, '_temp_candidates')
                return f"{best_temp:.1f} °C"
            
            # 如果没有找到主板温度，尝试备用方法
            self._debug_log("尝试备用方法获取主板温度")
            mobo_temp = self._extract_mobo_temp_fallback(sensors_output)
            if mobo_temp != "未知":
                return mobo_temp
            
            self._warning_log("未在sensors输出中找到主板温度")
            return "未知"
            
        except Exception as e:
            self._error_log(f"解析sensors主板温度输出失败: {e}")
            return "未知"

    def _extract_mobo_temp_fallback(self, sensors_output: str) -> str:
        """备用方法获取主板温度"""
        try:
            lines = sensors_output.split('\n')
            
            # 方法1: 查找非CPU的温度传感器
            for line in lines:
                line_lower = line.lower().strip()
                
                # 跳过明显的CPU相关行
                if any(cpu_keyword in line_lower for cpu_keyword in [
                    "cpu", "core", "package", "processor", "tctl", "tdie"
                ]):
                    continue
                
                # 查找温度值
                if '°c' in line_lower or ' c' in line_lower:
                    # 尝试提取温度值
                    import re
                    temp_match = re.search(r'(\d+\.?\d*)\s*[°]?\s*c', line_lower)
                    if temp_match:
                        temp_value = float(temp_match.group(1))
                        if 15 <= temp_value <= 60:  # 主板温度合理范围
                            self._info_log(f"通过备用方法获取主板温度: {temp_value:.1f}°C")
                            return f"{temp_value:.1f} °C"
            
            # 方法2: 查找hwmon设备中的主板温度
            for i, line in enumerate(lines):
                line_lower = line.lower()
                if "hwmon" in line_lower and "temp" in line_lower:
                    # 检查接下来的几行是否有温度值
                    for j in range(i+1, min(i+5, len(lines))):
                        next_line = lines[j].lower()
                        if '°c' in next_line or ' c' in next_line:
                            import re
                            temp_match = re.search(r'(\d+\.?\d*)\s*[°]?\s*c', next_line)
                            if temp_match:
                                temp_value = float(temp_match.group(1))
                                if 15 <= temp_value <= 60:
                                    self._info_log(f"通过hwmon获取主板温度: {temp_value:.1f}°C")
                                    return f"{temp_value:.1f} °C"
            
            return "未知"
            
        except Exception as e:
            self._debug_log(f"备用方法获取主板温度失败: {e}")
            return "未知"

    def format_uptime(self, seconds: float) -> str:
        """格式化运行时间为易读格式"""
        try:
            days, remainder = divmod(seconds, 86400)
            days, remainder = divmod(seconds, 86400)
            hours, remainder = divmod(remainder, 3600)
            minutes, seconds = divmod(remainder, 60)
            
            parts = []
            if days >= 1:
                parts.append(f"{int(days)}天")
            if hours >= 1:
                parts.append(f"{int(hours)}小时")
            if minutes >= 1 or not parts:  # 如果时间很短也要显示分钟
                parts.append(f"{int(minutes)}分钟")
                
            return " ".join(parts)
        except Exception as e:
            self.logger.error("Failed to format uptime: %s", str(e))
            return "未知"
    
    async def get_memory_info(self) -> dict:
        """获取内存使用信息"""
        try:
            # 使用 free 命令获取内存信息（-b 选项以字节为单位）
            mem_output = await self.coordinator.run_command("free -b")
            if not mem_output:
                return {}
            
            # 解析输出
            lines = mem_output.splitlines()
            if len(lines) < 2:
                return {}
                
            # 第二行是内存信息（Mem行）
            mem_line = lines[1].split()
            if len(mem_line) < 7:
                return {}
                
            return {
                "memory_total": int(mem_line[1]),
                "memory_used": int(mem_line[2]),
                "memory_available": int(mem_line[6])
            }
            
        except Exception as e:
            self._error_log(f"获取内存信息失败: {str(e)}")
            return {}
    
    async def get_vol_usage(self) -> dict:
        """获取 /vol* 开头的存储卷使用信息，避免唤醒休眠磁盘"""
        try:
            # 首先尝试智能检测活跃卷
            active_vols = await self.check_active_volumes()
            
            if active_vols:
                # 只查询活跃的卷，避免使用通配符可能唤醒所有磁盘
                vol_list = " ".join(active_vols)
                df_output = await self.coordinator.run_command(f"df -B 1 {vol_list} 2>/dev/null")
                if df_output:
                    result = self.parse_df_bytes(df_output)
                    if result:  # 确保有数据返回
                        return result
                
                df_output = await self.coordinator.run_command(f"df -h {vol_list} 2>/dev/null")
                if df_output:
                    result = self.parse_df_human_readable(df_output)
                    if result:  # 确保有数据返回
                        return result
            
            # 如果智能检测失败，回退到传统方法（仅在必要时）
            self._debug_log("智能卷检测无结果，回退到传统检测方法")
            
            # 优先使用字节单位，但添加错误处理
            df_output = await self.coordinator.run_command("df -B 1 /vol* 2>/dev/null || true")
            if df_output and "No such file or directory" not in df_output:
                result = self.parse_df_bytes(df_output)
                if result:
                    return result
            
            df_output = await self.coordinator.run_command("df -h /vol* 2>/dev/null || true")
            if df_output and "No such file or directory" not in df_output:
                result = self.parse_df_human_readable(df_output)
                if result:
                    return result
            
            # 最后的回退：尝试检测任何挂载的卷
            mount_output = await self.coordinator.run_command("mount | grep '/vol' || true")
            if mount_output:
                vol_points = []
                for line in mount_output.splitlines():
                    parts = line.split()
                    for part in parts:
                        if part.startswith('/vol') and part not in vol_points:
                            vol_points.append(part)
                
                if vol_points:
                    self._debug_log(f"从mount输出检测到卷: {vol_points}")
                    vol_list = " ".join(vol_points)
                    df_output = await self.coordinator.run_command(f"df -h {vol_list} 2>/dev/null || true")
                    if df_output:
                        return self.parse_df_human_readable(df_output)
            
            self._debug_log("所有存储卷检测方法都失败，返回空字典")
            return {}

        except Exception as e:
            self._error_log(f"获取存储卷信息失败: {str(e)}")
            return {}

    async def get_cpu_usage(self) -> dict:
        """获取CPU使用率（需要两次调用以计算差值）"""
        try:
            stat_output = await self.coordinator.run_command("cat /proc/stat 2>/dev/null || true")
            if not stat_output:
                return {"cpu_usage_percent": None}

            first_line = stat_output.splitlines()[0]
            parts = first_line.split()
            if len(parts) < 5 or parts[0] != "cpu":
                return {"cpu_usage_percent": None}

            user = int(parts[1])
            nice = int(parts[2])
            system = int(parts[3])
            idle = int(parts[4])
            iowait = int(parts[5]) if len(parts) > 5 else 0
            irq = int(parts[6]) if len(parts) > 6 else 0
            softirq = int(parts[7]) if len(parts) > 7 else 0

            total = user + nice + system + idle + iowait + irq + softirq
            active = total - idle

            return {
                "cpu_usage_percent": None,
                "_cpu_stat_total": total,
                "_cpu_stat_active": active,
                "_cpu_stat_idle": idle
            }
        except Exception as e:
            self._debug_log(f"获取CPU使用率失败: {e}")
            return {"cpu_usage_percent": None}

    @staticmethod
    def compute_cpu_percent(prev: dict | None, curr: dict | None) -> float | None:
        """根据两次/proc/stat快照计算CPU使用率百分比"""
        if not prev or not curr:
            return None
        prev_total = prev.get("_cpu_stat_total")
        prev_active = prev.get("_cpu_stat_active")
        curr_total = curr.get("_cpu_stat_total")
        curr_active = curr.get("_cpu_stat_active")
        if not all(isinstance(v, int) for v in [prev_total, prev_active, curr_total, curr_active]):
            return None
        delta_total = curr_total - prev_total
        delta_active = curr_active - prev_active
        if delta_total <= 0:
            return None
        return round(delta_active / delta_total * 100, 1)

    async def get_network_stats(self) -> dict:
        """获取网络接口流量统计"""
        try:
            net_output = await self.coordinator.run_command("cat /proc/net/dev 2>/dev/null || true")
            if not net_output:
                return {"interfaces": {}}

            interfaces = {}
            lines = net_output.splitlines()
            # 跳过标题行 (前两行)
            for line in lines[2:]:
                parts = line.strip().split()
                if len(parts) < 10:
                    continue
                iface = parts[0].rstrip(':')
                # 跳过回环接口
                if iface == "lo":
                    continue

                rx_bytes = int(parts[1])
                tx_bytes = int(parts[9])

                interfaces[iface] = {
                    "rx_bytes": rx_bytes,
                    "tx_bytes": tx_bytes
                }

            return {"interfaces": interfaces}
        except Exception as e:
            self._debug_log(f"获取网络统计失败: {e}")
            return {"interfaces": {}}

    async def check_active_volumes(self) -> list:
        """检查当前活跃的存储卷，避免唤醒休眠磁盘"""
        try:
            # 获取所有挂载点，这个操作不会访问磁盘内容
            mount_output = await self.coordinator.run_command("mount | grep '/vol' 2>/dev/null || true")
            if not mount_output:
                self._debug_log("未找到任何/vol挂载点")
                return []
            
            active_vols = []
            
            for line in mount_output.splitlines():
                if '/vol' in line:
                    # 提取挂载点
                    parts = line.split()
                    mount_point = None
                    
                    # 查找挂载点（通常在 'on' 关键词之后）
                    try:
                        on_index = parts.index('on')
                        if on_index + 1 < len(parts):
                            candidate = parts[on_index + 1]
                            # 严格检查是否以/vol开头
                            if candidate.startswith('/vol'):
                                mount_point = candidate
                    except ValueError:
                        # 如果没有 'on' 关键词，查找以/vol开头的部分
                        for part in parts:
                            if part.startswith('/vol'):
                                mount_point = part
                                break
                    
                    # 过滤挂载点：只保留根级别的/vol*挂载点
                    if mount_point and self.is_root_vol_mount(mount_point):
                        # 检查这个卷对应的磁盘是否活跃
                        is_active = await self.is_volume_disk_active(mount_point)
                        if is_active:
                            active_vols.append(mount_point)
                            self._debug_log(f"添加活跃卷: {mount_point}")
                        else:
                            # 即使磁盘不活跃，也添加到列表中，但标记为可能休眠
                            # 这样可以保证有基本的存储信息
                            active_vols.append(mount_point)
                            self._debug_log(f"卷 {mount_point} 对应磁盘可能休眠，但仍包含在检测中")
                    else:
                        self._debug_log(f"跳过非根级别vol挂载点: {mount_point}")
            
            # 去重并排序
            active_vols = sorted(list(set(active_vols)))
            self._debug_log(f"最终检测到的根级别/vol存储卷: {active_vols}")
            return active_vols
            
        except Exception as e:
            self._debug_log(f"检查活跃存储卷失败: {e}")
            return []
    
    def is_root_vol_mount(self, mount_point: str) -> bool:
        """检查是否为根级别的/vol挂载点"""
        if not mount_point or not mount_point.startswith('/vol'):
            return False
        
        # 移除开头的/vol部分进行分析
        remainder = mount_point[4:]  # 去掉'/vol'
        
        # 如果remainder为空，说明是/vol，这是根级别
        if not remainder:
            return True
        
        # 如果remainder只是数字（如/vol1, /vol2），这是根级别
        if remainder.isdigit():
            return True
        
        # 如果remainder是单个字母或字母数字组合且没有斜杠，也认为是根级别
        # 例如：/vola, /volb, /vol1a 等
        if '/' not in remainder and len(remainder) <= 3:
            return True
        
        # 其他情况都认为是子目录，如：
        # /vol1/docker/overlay2/...
        # /vol1/data/...
        # /vol1/config/...
        self._debug_log(f"检测到子目录挂载点: {mount_point}")
        return False

    def parse_df_bytes(self, df_output: str) -> dict:
        """解析df命令的字节输出"""
        volumes = {}
        try:
            for line in df_output.splitlines()[1:]:  # 跳过标题行
                parts = line.split()
                if len(parts) < 6:
                    continue
                    
                mount_point = parts[-1]
                # 严格检查只处理根级别的 /vol 挂载点
                if not self.is_root_vol_mount(mount_point):
                    self._debug_log(f"跳过非根级别vol挂载点: {mount_point}")
                    continue
                    
                try:
                    size_bytes = int(parts[1])
                    used_bytes = int(parts[2])
                    avail_bytes = int(parts[3])
                    use_percent = parts[4]
                    
                    def bytes_to_human(b):
                        for unit in ['', 'K', 'M', 'G', 'T']:
                            if abs(b) < 1024.0:
                                return f"{b:.1f}{unit}"
                            b /= 1024.0
                        return f"{b:.1f}P"
                    
                    volumes[mount_point] = {
                        "filesystem": parts[0],
                        "size": bytes_to_human(size_bytes),
                        "used": bytes_to_human(used_bytes),
                        "available": bytes_to_human(avail_bytes),
                        "use_percent": use_percent
                    }
                    self._debug_log(f"添加根级别/vol存储卷信息: {mount_point}")
                except (ValueError, IndexError) as e:
                    self._debug_log(f"解析存储卷行失败: {line} - {str(e)}")
                    continue
        except Exception as e:
            self._error_log(f"解析df字节输出失败: {e}")
            
        return volumes
    
    def parse_df_human_readable(self, df_output: str) -> dict:
        """解析df命令输出"""
        volumes = {}
        try:
            for line in df_output.splitlines()[1:]:  # 跳过标题行
                parts = line.split()
                if len(parts) < 6:
                    continue
                    
                mount_point = parts[-1]
                # 严格检查只处理根级别的 /vol 挂载点
                if not self.is_root_vol_mount(mount_point):
                    self._debug_log(f"跳过非根级别vol挂载点: {mount_point}")
                    continue
                    
                try:
                    size = parts[1]
                    used = parts[2]
                    avail = parts[3]
                    use_percent = parts[4]
                    
                    volumes[mount_point] = {
                        "filesystem": parts[0],
                        "size": size,
                        "used": used,
                        "available": avail,
                        "use_percent": use_percent
                    }
                    self._debug_log(f"添加根级别/vol存储卷信息: {mount_point}")
                except (ValueError, IndexError) as e:
                    self._debug_log(f"解析存储卷行失败: {line} - {str(e)}")
                    continue
        except Exception as e:
            self._error_log(f"解析df输出失败: {e}")
                
        return volumes
    
    async def reboot_system(self):
        """重启系统"""
        self._info_log("Initiating system reboot...")
        try:
            await self.coordinator.run_command("sudo reboot")
            self._info_log("Reboot command sent")
            
            if "system" in self.coordinator.data:
                self.coordinator.data["system"]["status"] = "rebooting"
                self.coordinator.async_update_listeners()
        except Exception as e:
            self._error_log(f"Failed to reboot system: {str(e)}")
            raise
    
    async def shutdown_system(self):
        """关闭系统"""
        self._info_log("Initiating system shutdown...")
        try:
            await self.coordinator.run_command("sudo shutdown -h now")
            self._info_log("Shutdown command sent")
            
            if "system" in self.coordinator.data:
                self.coordinator.data["system"]["status"] = "off"
                self.coordinator.async_update_listeners()
        except Exception as e:
            self._error_log(f"Failed to shutdown system: {str(e)}")
            raise