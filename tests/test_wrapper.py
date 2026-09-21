# SPDX-License-Identifier: GPL-2.0-only
#
# Copyright (C) 2026 Dario Faggioli
# Copyright (C) 2026 SUSE LLC
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

import pytest
from lxml import etree as ET
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))
from vnuma_wrapper import inject_topology, extract_vm_params, serialize_libvirt_xml

def xml_factory(vcpus=4, mem_kib=8388608, hp_size=None, existing_numa=False):
    """Generate a template XML to be used for the tests."""
    hp_block = f"""
    <memoryBacking>
      <hugepages>
        <page size='{hp_size}' unit='KiB' nodeset='0'/>
      </hugepages>
    </memoryBacking>""" if hp_size else ""

    cpu_block = """
    <cpu mode='host-passthrough'>
      <numa><cell id='0' cpus='0-3' memory='8388608' unit='KiB'/></numa>
    </cpu>""" if existing_numa else "<cpu mode='host-passthrough'/>"

    xml = f"""
    <domain type='kvm'>
      <name>testvm</name>
      <memory unit='KiB'>{mem_kib}</memory>
      <currentMemory unit='KiB'>{mem_kib}</currentMemory>
      <vcpu placement='static'>{vcpus}</vcpu>
      {hp_block}
      {cpu_block}
    </domain>"""
    return ET.fromstring(xml.strip(), parser=ET.XMLParser(remove_blank_text=False))

def mock_sysfs_mapper(node_id):
    """Simulate the reading of /sys/devices/system/node/nodeX/cpulist for multiple nodes."""
    topology = {
        0: "0-7",
        1: "8-15",
        2: "16-23",
        3: "24-31"
    }
    return topology.get(node_id, "0")

def test_extract_vm_params_no_hp():
    root = xml_factory(vcpus=8, mem_kib=16777216)
    vcpus, mem, hp = extract_vm_params(root)
    assert vcpus == 8
    assert mem == 16777216
    assert hp == 0

def test_extract_vm_params_with_hp_and_stripping():
    root = xml_factory(vcpus=4, mem_kib=8388608, hp_size=1048576) # 1GB pages
    vcpus, mem, hp = extract_vm_params(root)
    assert hp == 1024 # Converted in MB
    page = root.xpath("//memoryBacking/hugepages/page")[0]
    assert "nodeset" not in page.attrib

def test_inject_topology_single_node():
    root = xml_factory(vcpus=4, mem_kib=8388608)
    pnuma_str = "0"

    inject_topology(root, pnuma_str, mock_sysfs_mapper)

    cells = root.xpath("./cpu/numa/cell")
    assert len(cells) == 1
    assert cells[0].get("cpus") == "0-3"
    assert int(cells[0].get("memory")) == 8388608
    assert root.xpath("./numatune/memory")[0].get("nodeset") == "0"
    assert len(root.xpath("./cputune/vcpupin")) == 4

def test_inject_topology_two_nodes():
    root = xml_factory(vcpus=8, mem_kib=16777216)
    pnuma_str = "0,1"

    inject_topology(root, pnuma_str, mock_sysfs_mapper)

    cells = root.xpath("./cpu/numa/cell")
    assert len(cells) == 2
    assert cells[0].get("cpus") == "0-3"
    assert cells[1].get("cpus") == "4-7"
    assert int(cells[0].get("memory")) == 16777216 // 2
    assert root.xpath("./numatune/memory")[0].get("nodeset") == "0,1"
    vcpupins = root.xpath("./cputune/vcpupin")
    assert len(vcpupins) == 8
    assert vcpupins[0].get("cpuset") == "0-7"
    assert vcpupins[7].get("cpuset") == "8-15"

def test_inject_topology_four_nodes():
    root = xml_factory(vcpus=16, mem_kib=33554432)
    pnuma_str = "0-3"

    inject_topology(root, pnuma_str, mock_sysfs_mapper)

    cells = root.xpath("./cpu/numa/cell")
    assert len(cells) == 4
    assert cells[0].get("cpus") == "0-3"
    assert cells[3].get("cpus") == "12-15"
    assert int(cells[0].get("memory")) == 33554432 // 4
    assert root.xpath("./numatune/memory")[0].get("nodeset") == "0-3"

    memnodes = root.xpath("./numatune/memnode")
    assert len(memnodes) == 4
    assert memnodes[3].get("cellid") == "3"
    assert memnodes[3].get("nodeset") == "3"

    vcpupins = root.xpath("./cputune/vcpupin")
    assert len(vcpupins) == 16
    assert vcpupins[15].get("cpuset") == "24-31"

def test_inject_topology_cleans_existing():
    root = xml_factory(vcpus=4, mem_kib=4096, existing_numa=True)
    inject_topology(root, "1", mock_sysfs_mapper)

    cells = root.xpath("./cpu/numa/cell")
    assert len(cells) == 1
    assert root.xpath("./numatune/memnode")[0].get("nodeset") == "1"

def test_serialize_quotes():
    root = xml_factory()
    xml_str = serialize_libvirt_xml(root)
    assert "type='kvm'" in xml_str
    assert 'type="kvm"' not in xml_str
