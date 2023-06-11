#!/usr/bin/perl -w
# Quick script for introspecting on short HTTP requests from Particle Photon or Arduino microcontroller
# (since brew install netpipes doesn't help me because netpipes isn't a brew package :-( )
use IO::Socket::INET;
 
# auto-flush on socket
$| = 1;
 
# creating a listening socket
my $socket = new IO::Socket::INET (
    LocalHost => '0.0.0.0',
    LocalPort => '3001',
    Proto => 'tcp',
    Listen => 5,
    Reuse => 1
);
die "cannot create socket $!\n" unless $socket;
print "server waiting for client connection on port 3001\n";
 
while(1)
{
    # waiting for a new client connection
    my $client_socket = $socket->accept();
 
    # get information about a newly connected client
    my $client_address = $client_socket->peerhost();
    my $client_port = $client_socket->peerport();
    print "connection from $client_address:$client_port\n";
 
    # read up to 1024 characters from the connected client
    my $data = "";
    $client_socket->recv($data, 1024);
    print "received data: $data\n";
    $client_socket->recv($data, 1024);
    print "received data: $data\n";
    $client_socket->recv($data, 1024);
    print "received data: $data\n";
    $client_socket->recv($data, 1024);
    print "received data: $data\n";
 
    # notify client that response has been sent
    shutdown($client_socket, 1);
}
 
$socket->close();
