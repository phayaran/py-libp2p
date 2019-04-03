import asyncio
import multiaddr
import pytest
import random
import pprint

from pyvis.network import Network
from tests.utils import cleanup
from libp2p import new_node
from libp2p.peer.peerinfo import info_from_p2p_addr
from libp2p.pubsub.pb import rpc_pb2
from libp2p.pubsub.pubsub import Pubsub
from libp2p.pubsub.floodsub import FloodSub
from utils import generate_message_id, generate_RPC_packet

# pylint: disable=too-many-locals

async def connect(node1, node2):
    """
    Connect node1 to node2
    """
    addr = node2.get_addrs()[0]
    info = info_from_p2p_addr(addr)
    await node1.connect(info)

async def perform_test_from_obj(obj,timeout_len=2):
    """
    Perform a floodsub test from a test obj.
    test obj are composed as follows:
    
    {
        "supported_protocols": ["supported/protocol/1.0.0",...],
        "adj_list": {
            "node1": ["neighbor1_of_node1", "neighbor2_of_node1", ...],
            "node2": ["neighbor1_of_node2", "neighbor2_of_node2", ...],
            ...
        },
        "topic_map": {
            "topic1": ["node1_subscribed_to_topic1", "node2_subscribed_to_topic1", ...]
        },
        "messages": [
            {
                "topics": ["topic1_for_message", "topic2_for_message", ...],
                "data": "some contents of the message (newlines are not supported)",
                "node_id": "message sender node id"
            },
            ...
        ]
    }
    NOTE: In adj_list, for any neighbors A and B, only list B as a neighbor of A
    or B as a neighbor of A once. Do NOT list both A: ["B"] and B:["A"] as the behavior
    is undefined (even if it may work)
    """

    # Step 1) Create graph
    adj_list = obj["adj_list"]
    node_map = {}
    floodsub_map = {}
    pubsub_map = {}

    supported_protocols = obj["supported_protocols"]

    tasks_connect = []
    for start_node_id in adj_list:
        # Create node if node does not yet exist
        if start_node_id not in node_map:
            node = await new_node(transport_opt=["/ip4/127.0.0.1/tcp/0"])
            await node.get_network().listen(multiaddr.Multiaddr("/ip4/127.0.0.1/tcp/0"))

            node_map[start_node_id] = node

            floodsub = FloodSub(supported_protocols)
            floodsub_map[start_node_id] = floodsub
            pubsub = Pubsub(node, floodsub, start_node_id)
            pubsub_map[start_node_id] = pubsub

        # For each neighbor of start_node, create if does not yet exist,
        # then connect start_node to neighbor
        for neighbor_id in adj_list[start_node_id]:
            # Create neighbor if neighbor does not yet exist
            if neighbor_id not in node_map:
                neighbor_node = await new_node(transport_opt=["/ip4/127.0.0.1/tcp/0"])
                await neighbor_node.get_network().listen(multiaddr.Multiaddr("/ip4/127.0.0.1/tcp/0"))
                
                node_map[neighbor_id] = neighbor_node

                floodsub = FloodSub(supported_protocols)
                floodsub_map[neighbor_id] = floodsub
                pubsub = Pubsub(neighbor_node, floodsub, neighbor_id)
                pubsub_map[neighbor_id] = pubsub

            # Connect node and neighbor
            # await connect(node_map[start_node_id], node_map[neighbor_id])
            tasks_connect.append(asyncio.ensure_future(connect(node_map[start_node_id], node_map[neighbor_id])))
    tasks_connect.append(asyncio.sleep(2))
    await asyncio.gather(*tasks_connect)

    # Allow time for graph creation before continuing
    # await asyncio.sleep(0.25)

    # Step 2) Subscribe to topics
    queues_map = {}
    topic_map = obj["topic_map"]

    tasks_topic = []
    tasks_topic_data = []
    for topic in topic_map:
        for node_id in topic_map[topic]:
            """
            # Subscribe node to topic
            q = await pubsub_map[node_id].subscribe(topic)

            # Create topic-queue map for node_id if one does not yet exist
            if node_id not in queues_map:
                queues_map[node_id] = {}

            # Store queue in topic-queue map for node
            queues_map[node_id][topic] = q
            """
            tasks_topic.append(asyncio.ensure_future(pubsub_map[node_id].subscribe(topic)))
            tasks_topic_data.append((node_id, topic))
    tasks_topic.append(asyncio.sleep(2))

    # Gather is like Promise.all
    responses = await asyncio.gather(*tasks_topic, return_exceptions=True)
    for i in range(len(responses) - 1):
        q = responses[i]
        node_id, topic = tasks_topic_data[i]
        if node_id not in queues_map:
            queues_map[node_id] = {}

        # Store queue in topic-queue map for node
        queues_map[node_id][topic] = q

    # Allow time for subscribing before continuing
    # await asyncio.sleep(0.01)

    # Step 3) Publish messages
    topics_in_msgs_ordered = []
    messages = obj["messages"]
    tasks_publish = []

    all_actual_msgs = {}
    for msg in messages:
        topics = msg["topics"]

        data = msg["data"]
        node_id = msg["node_id"]

        # Get actual id for sender node (not the id from the test obj)
        actual_node_id = str(node_map[node_id].get_id())

        # Create correctly formatted message
        msg_talk = generate_RPC_packet(actual_node_id, topics, data, generate_message_id())

        # Publish message
        # await floodsub_map[node_id].publish(actual_node_id, msg_talk.to_str())
        tasks_publish.append(asyncio.ensure_future(floodsub_map[node_id].publish(\
            actual_node_id, msg_talk.SerializeToString())))

        # For each topic in topics, add topic, msg_talk tuple to ordered test list
        # TODO: Update message sender to be correct message sender before
        # adding msg_talk to this list
        for topic in topics:
            if topic in all_actual_msgs:
                all_actual_msgs[topic].append(msg_talk.publish[0].SerializeToString())
            else:
                all_actual_msgs[topic] = [msg_talk.publish[0].SerializeToString()]

    # Allow time for publishing before continuing
    # await asyncio.sleep(0.4)
    tasks_publish.append(asyncio.sleep(2))
    await asyncio.gather(*tasks_publish)

    # Step 4) Check that all messages were received correctly.
    for topic in all_actual_msgs:
        for node_id in topic_map[topic]:
            all_received_msgs_in_topic = []

            # Add all messages to message received list for given node in given topic
            while (queues_map[node_id][topic].qsize() > 0):
                # Get message from subscription queue
                msg_on_node = (await queues_map[node_id][topic].get()).SerializeToString() 
                all_received_msgs_in_topic.append(msg_on_node)

            # Ensure each message received was the same as one sent
            for msg_on_node in all_received_msgs_in_topic:
                assert msg_on_node in all_actual_msgs[topic]

            # Ensure same number of messages received as sent
            assert len(all_received_msgs_in_topic) == len(all_actual_msgs[topic])

def generate_test_obj_with_random_params(params):
    return {
        "num_nodes": random.randint(params["min_num_nodes"], params["max_num_nodes"]),
        "density": random.uniform(0.01, params["max_density"]),
        "num_topics": random.randint(1, params["max_num_topics"]),
        "max_nodes_per_topic": random.randint(params["min_max_nodes_per_topic"], params["max_max_nodes_per_topic"]),
        "max_msgs_per_topic": random.randint(params["min_max_msgs_per_topic"], params["max_max_msgs_per_topic"])
    }

def generate_random_topology(num_nodes, density, num_topics, max_nodes_per_topic, max_msgs_per_topic):
    # Give nodes string labels so that perform_test_with_obj works correctly
    # Note: "n" is appended so that visualizations work properly ('00' caused issues)
    nodes = ["n" + str(i).zfill(2) for i in range(0,num_nodes)]

    # Adjust max_nodes_per_topic if it exceeds number of nodes
    max_nodes_per_topic = min(max_nodes_per_topic, num_nodes)

    # 1) Generate random graph structure

    # Create initial graph by connecting each node to its previous node
    # This ensures the graph is connected
    graph = {}

    graph[nodes[0]] = []

    max_num_edges = num_nodes * (num_nodes - 1) / 2
    num_edges = 0

    for i in range(1, len(nodes)):
        prev = nodes[i - 1]
        curr = nodes[i]

        graph[curr] = [prev]
        graph[prev].append(curr)
        num_edges += 1

    # Add random edges until density is hit
    while num_edges / max_num_edges < density:
        selected_nodes = random.sample(nodes, 2)

        # Only add the nodes as neighbors if they are not already neighbors
        if selected_nodes[0] not in graph[selected_nodes[1]]:
            graph[selected_nodes[0]].append(selected_nodes[1])
            graph[selected_nodes[1]].append(selected_nodes[0])
            num_edges += 1

    # 2) Pick num_topics random nodes to perform random walks at 
    nodes_to_start_topics_from = random.sample(nodes, num_topics)

    nodes_in_topic_list = []
    for node in nodes_to_start_topics_from:
        nodes_walked = []
        curr = node
        nodes_walked.append(curr)

        # TODO: Pick random num of nodes per topic
        while len(nodes_walked) < max_nodes_per_topic:
            # Pick a random neighbor of curr to walk to
            neighbors = graph[curr]
            rand_num = random.randint(0, len(neighbors) - 1)
            neighbor = neighbors[rand_num]
            curr = neighbor
            if curr not in nodes_walked:
                nodes_walked.append(curr)

        nodes_in_topic_list.append(nodes_walked)

    # 3) Start creating test_obj
    test_obj = {"supported_protocols": ["/floodsub/1.0.0"]}
    test_obj["adj_list"] = graph
    test_obj["topic_map"] = {}
    for i in range(len(nodes_in_topic_list)):
        test_obj["topic_map"][str(i)] = nodes_in_topic_list[i]

    # 4) Finish creating test_obj by adding messages at random start nodes in each topic
    test_obj["messages"] = []
    for i in range(len(nodes_in_topic_list)):
        nodes_in_topic = nodes_in_topic_list[i]
        rand_num = random.randint(0, len(nodes_in_topic) - 1)
        start_node = nodes_in_topic[rand_num]
        for j in range(max_msgs_per_topic):
            test_obj["messages"].append({
                    "topics": [str(i)],
                    "data": str(random.randint(0, 100000)),
                    "node_id": str(start_node)
                })

    # 5) Return completed test_obj
    return test_obj

def create_graph(test_obj):
    net = Network()
    net.barnes_hut()

    adj_list = test_obj["adj_list"]
    # print(list(adj_list.keys()))
    nodes_to_add = list(adj_list.keys())
    net.add_nodes(nodes_to_add)
    for node in adj_list:
        neighbors = adj_list[node]
        for neighbor in neighbors:
            net.add_edge(node, neighbor)

    net.show("random_topology.html")

@pytest.mark.asyncio
async def test_simple_random():
    num_nodes = 5
    density = 1
    num_topics = 2
    max_nodes_per_topic = 5
    max_msgs_per_topic = 5
    print("Generating random topology")
    topology_test_obj = generate_random_topology(num_nodes, density, num_topics,\
        max_nodes_per_topic, max_msgs_per_topic)

    print("*****Topology Summary*****")
    print("# nodes: " + str(num_nodes))
    print("Density: " + str(density))
    print("# topics: " + str(num_topics))
    print("Nodes per topic: " + str(max_nodes_per_topic))
    print("Msgs per topic: " + str(max_msgs_per_topic))
    print("**************************")

    print("Performing Test")
    await perform_test_from_obj(topology_test_obj, timeout_len=20)
    print("Test Completed")
    print("Generating Graph")
    create_graph(topology_test_obj)
    print("Graph Generated")

    # Success, terminate pending tasks.
    await cleanup()

@pytest.mark.asyncio
async def test_random_10():
    min_num_nodes = 8
    max_num_nodes = 10
    max_density = 0.4
    max_num_topics = 5
    min_max_nodes_per_topic = 10
    max_max_nodes_per_topic = 20
    min_max_msgs_per_topic = 10
    max_max_msgs_per_topic = 20

    num_random_tests = 10

    summaries = []

    params_to_generate_random_params = {
                "min_num_nodes": min_num_nodes,
                "max_num_nodes": max_num_nodes,
                "max_density": max_density,
                "max_num_topics": max_num_topics,
                "min_max_nodes_per_topic": min_max_nodes_per_topic,
                "max_max_nodes_per_topic": max_max_nodes_per_topic,
                "min_max_msgs_per_topic": min_max_msgs_per_topic,
                "max_max_msgs_per_topic": max_max_msgs_per_topic
            }

    for i in range(0, num_random_tests):
        random_params = generate_test_obj_with_random_params(params_to_generate_random_params)

        print("Generating random topology")
        topology_test_obj = generate_random_topology(random_params["num_nodes"], random_params["density"],\
            random_params["num_topics"], random_params["max_nodes_per_topic"], \
            random_params["max_msgs_per_topic"])

        summary = {
            "num_nodes": random_params["num_nodes"],
            "density": random_params["density"],
            "num_topics": random_params["num_topics"],
            "nodes_per_topics": random_params["max_nodes_per_topic"],
            "msgs_per_topics": random_params["max_msgs_per_topic"]
        }
        summaries.append(pprint.pformat(summary, indent=4))

        print("Performing Test")
        await perform_test_from_obj(topology_test_obj, timeout_len=30)
        print("Test Completed")
        # print("Generating Graph")
        # create_graph(topology_test_obj)
        # print("Graph Generated")
        print("***Test " + str(i + 1) + "/" + str(num_random_tests) + " Completed***")

    with open('summaries.rand_test', 'a') as out_file:
        out_file.write(pprint.pformat(summaries, indent=4))

    # Success, terminate pending tasks.
    await cleanup()
