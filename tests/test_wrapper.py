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
    """Simulate the reading of /sys/devices/system/node/nodeX/cpulist"""
    topology = {
        0: "0-15,64-79",
        1: "16-31,80-95"
    }
    return topology.get(node_id, "0")

def test_extract_vm_params_no_hp():
    root = xml_factory(vcpus=8, mem_kib=16777216)
    vcpus, mem, hp = extract_vm_params(root)
    assert vcpus == 8
    assert mem == 16777216
    assert hp == 0

def test_extract_vm_params_with_hp_and_stripping():
    # Check that nodeset, if present, is removed
    root = xml_factory(vcpus=4, mem_kib=8388608, hp_size=1048576) # 1GB pages
    vcpus, mem, hp = extract_vm_params(root)
    assert hp == 1024 # Converted in MB
    page = root.xpath("//memoryBacking/hugepages/page")[0]
    assert "nodeset" not in page.attrib

def test_inject_topology_two_nodes():
    root = xml_factory(vcpus=8, mem_kib=16777216)
    pnuma_str = "0,1" # numa-preplace suggests 2 nodes
    
    inject_topology(root, pnuma_str, mock_sysfs_mapper)
    
    # 1. Verify vNUMA cells
    cells = root.xpath("./cpu/numa/cell")
    assert len(cells) == 2
    assert cells[0].get("cpus") == "0-3"
    assert cells[1].get("cpus") == "4-7"
    assert int(cells[0].get("memory")) == 16777216 // 2
    
    # 2. Verify numatune mbind
    assert root.xpath("./numatune/memory")[0].get("nodeset") == "0,1"
    memnodes = root.xpath("./numatune/memnode")
    assert len(memnodes) == 2
    assert memnodes[0].get("nodeset") == "0"
    
    # 3. Verify vcpu pinning
    vcpupins = root.xpath("./cputune/vcpupin")
    assert len(vcpupins) == 8
    assert vcpupins[0].get("cpuset") == "0-15,64-79"
    assert vcpupins[7].get("cpuset") == "16-31,80-95"

def test_inject_topology_cleans_existing():
    root = xml_factory(vcpus=4, mem_kib=4096, existing_numa=True)
    inject_topology(root, "1", mock_sysfs_mapper)
    
    # Check that old topology, if present, is gone
    cells = root.xpath("./cpu/numa/cell")
    assert len(cells) == 1
    assert root.xpath("./numatune/memnode")[0].get("nodeset") == "1"

def test_serialize_quotes():
    root = xml_factory()
    xml_str = serialize_libvirt_xml(root)
    # Some check about XML formatting...
    assert "type='kvm'" in xml_str
    assert 'type="kvm"' not in xml_str
