#main sh
gnome-terminal -e 'bash -c "sh node_11.sh;exec bash"'
echo "run circle_1 node 1"
gnome-terminal -e 'bash -c "sh node_12.sh;exec bash"'
echo "run circle_1 node 2"
gnome-terminal -e 'bash -c "sh node_13.sh;exec bash"'
echo "run circle_1 node 3"


gnome-terminal -e 'bash -c "sh node_21.sh;exec bash"'
echo "run circle_2 node 1"
gnome-terminal -e 'bash -c "sh node_22.sh;exec bash"'
echo "run circle_2 node 2"
gnome-terminal -e 'bash -c "sh node_23.sh;exec bash"'
echo "run circle_2 node 3"

echo "Start Global Federated Learning Framework"
gnome-terminal -e 'bash -c "sh federated.sh;exec bash"'